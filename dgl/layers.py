import torch as th
import numpy as np
import torch.nn as nn
from torch.nn import Softplus


class AtomEmbedding(nn.Module):
    """
    Convert the atom(node) list to atom embeddings.
    The atom with the same element share the same inital embeddding.
    """

    def __init__(self, dim=128, type_num=100, pre_train=None):
        """
        Randomly init the element embeddings.
        Args:
            dim: the dim of embeddings
            type_num: the largest atomic number of atoms in the dataset
            pre_train: the pre_trained embeddings
        """
        super().__init__()
        self._dim = dim
        self._type_num = type_num
        if pre_train is not None:
            self.embedding = nn.Embedding.from_pretrained(pre_train,
                                                          padding_idx=0)
        else:
            self.embedding = nn.Embedding(type_num, dim, padding_idx=0)

    def forward(self, g, p_name="node"):
        """Input is the dgl graph"""
        atom_list = g.ndata["node_type"]
        g.ndata[p_name] = self.embedding(atom_list)
        return g.ndata[p_name]


class EdgeEmbedding(nn.Module):
    """
    Convert the edge to embedding.
   The edge links same pair of atoms share the same inital embedding.
    """

    def __init__(self, dim=128, edge_num=300, pre_train=None):
        """
        Randomly init the edge embeddings.
        Args:
            dim: the dim of embeddings
            edge_num: the maximum type of edges
            pre_train: the pre_trained embeddings
        """
        super().__init__()
        self._dim = dim
        self._edge_num = edge_num
        if pre_train is not None:
            self.embedding = nn.Embedding.from_pretrained(pre_train,
                                                          padding_idx=0)
        else:
            self.embedding = nn.Embedding(edge_num, dim, padding_idx=0)

    def generate_edge_type(self, edges):
        """
        Generate the edge type based on the src&dst atom type of the edge.
        Note that C-O and O-C are the same edge type.
        Note that current elements only contains H,C,N,O,F,
            so that the edge type could be hashed as the multiply of atom-type
            without conflict.
            When more kinds of elements are included, this implementation could
            be wrong. e.g. The multiply of C-C and Be-F are both 36.
        """
        atom_type_x = edges.src["node_type"]
        atom_type_y = edges.dst["node_type"]

        return {"type": atom_type_x * atom_type_y}

    def forward(self, g, p_name="edge_f", raw_type=False):
        if not raw_type:
            g.apply_edges(self.generate_edge_type)
        g.edata[p_name] = self.embedding(g.edata["type"])
        return g.edata[p_name]


class ShiftSoftplus(Softplus):
    """
    Shiftsoft plus activation function:
        1/beta * (log(1 + exp**(beta * x)) - log(shift))
    """

    def __init__(self, beta=1, shift=2, threshold=20):
        super().__init__(beta, threshold)
        self.shift = shift
        self.softplus = Softplus(beta, threshold)

    def forward(self, input):
        return self.softplus(input) - np.log(float(self.shift))


class RBFLayer(nn.Module):
    """
    Radial basis functions Layer.
    e(d) = exp(- gamma * ||d - mu_k||^2)
    gamma = 10
    0 <= mu_k <= 30 for k=1~300
    """

    def __init__(self, low=0, high=30, gap=0.1, dim=1, coef="p"):
        super().__init__()
        self._low = low
        self._high = high
        self._gap = gap
        self._dim = dim

        self._n_centers = int(np.ceil((high - low) / gap))
        centers = np.linspace(low, high, self._n_centers)
        self.centers = th.tensor(centers, dtype=th.float, requires_grad=False)
        self.centers = nn.Parameter(self.centers, requires_grad=False)
        self._fan_out = self._dim * self._n_centers
        self.coef = coef

        self._gap = centers[1] - centers[0]

    def dis2rbf(self, edges):
        dist = edges.data["distance"]
        radial = dist - self.centers
        if self.coef == "p":
            coef = -1 / self._gap
        else:
            coef = -0.5 / (self._gap**2)
        rbf = th.exp(coef * (radial**2))
        return {"rbf": rbf}

    def forward(self, g):
        """Convert distance scalr to rbf vector"""
        g.apply_edges(self.dis2rbf)
        return g.edata["rbf"]


class CFConv(nn.Module):
    """
    The continuous-filter convolution layer in SchNet.
    One CFConv contains one rbf layer and three linear layer
        (two of them have activation funct).
    """

    def __init__(self, rbf_dim, dim=64, act="sp"):
        """
        Args:
            dim: the dimension of linear layers
            act: activation function (default shifted softplus)
        """
        super().__init__()
        self._rbf_dim = rbf_dim
        self._dim = dim

        self.linear_layer1 = nn.Linear(self._rbf_dim, self._dim)
        self.linear_layer2 = nn.Linear(self._dim, self._dim)

        if act == "sp":
            self.activation = nn.Softplus(beta=0.5, threshold=14)
        else:
            self.activation = act

    def update_edge(self, edges):
        rbf = edges.data["rbf"]
        h = self.linear_layer1(rbf)
        h = self.activation(h)
        h = self.linear_layer2(h)
        # h = self.activation(h)
        return {"h": h}

    def send_edge_info(self, edges):
        update_info = edges.src["new_node"] * edges.data["h"]
        return {"neighbor_info": update_info}

    def elemnet_wise_gather(self, nodes):
        """
        Implement element-wise multiplication (node x edge)
        and reduce (sum) to nodes.
        """
        new_node = th.sum(nodes.mailbox["neighbor_info"], dim=1)
        return {"new_node": new_node}

    def forward(self, g):
        g.apply_edges(self.update_edge)
        g.send_and_recv(g.edges(),
                        message_func=self.send_edge_info,
                        reduce_func=self.elemnet_wise_gather)
        return g.ndata["new_node"]


class Interaction(nn.Module):
    """
    The interaction layer in the SchNet model.
    """

    def __init__(self, rbf_dim, dim):
        super().__init__()
        self._node_dim = dim
        self.activation = nn.Softplus(beta=0.5, threshold=14)
        self.node_layer1 = nn.Linear(dim, dim, bias=False)
        self.cfconv = CFConv(rbf_dim, dim, act=self.activation)
        self.node_layer2 = nn.Linear(dim, dim)
        self.node_layer3 = nn.Linear(dim, dim)

    def forward(self, g):

        g.ndata["new_node"] = self.node_layer1(g.ndata["node"])
        cf_node = self.cfconv(g)
        cf_node_1 = self.node_layer2(cf_node)
        cf_node_1a = self.activation(cf_node_1)
        new_node = self.node_layer3(cf_node_1a)
        g.ndata["node"] = g.ndata["node"] + new_node
        return g.ndata["node"]


class VEConv(nn.Module):
    """
    The Vertex-Edge convolution layer in MGCN which take edge & vertex features
    in consideratoin at the same time.
    """

    def __init__(self, rbf_dim, dim=64, update_edge=True):
        """
        Args:
            dim: the dimension of linear layers
            act: activation function (default shifted softplus)
        """
        super().__init__()
        self._rbf_dim = rbf_dim
        self._dim = dim
        self._update_edge = update_edge

        self.linear_layer1 = nn.Linear(self._rbf_dim, self._dim)
        self.linear_layer2 = nn.Linear(self._dim, self._dim)
        self.linear_layer3 = nn.Linear(self._dim, self._dim)

        self.activation = nn.Softplus(beta=0.5, threshold=14)

    def update_rbf(self, edges):
        rbf = edges.data["rbf"]
        h = self.linear_layer1(rbf)
        h = self.activation(h)
        h = self.linear_layer2(h)
        return {"h": h}

    def update_edge(self, edges):
        edge_f = edges.data["edge_f"]
        h = self.linear_layer3(edge_f)
        return {"edge_f": h}

    def send_edge_info(self, edges):
        update_info = edges.src["new_node"] * edges.data["h"] + edges.data[
            "edge_f"]
        return {"neighbor_info": update_info}

    def elemnet_wise_gather(self, nodes):
        """
        Implement element-wise multiplication (node x edge)
        and reduce (sum) to nodes.
        """
        new_node = th.sum(nodes.mailbox["neighbor_info"], dim=1)
        return {"new_node": new_node}

    def forward(self, g):
        g.apply_edges(self.update_rbf)
        if self._update_edge:
            g.apply_edges(self.update_edge)

        g.send_and_recv(g.edges(),
                        message_func=self.send_edge_info,
                        reduce_func=self.elemnet_wise_gather)
        return g.ndata["new_node"]


class MultiLevelInteraction(nn.Module):
    """
    The multilevel interaction in the MGCN model.
    """

    def __init__(self, rbf_dim, dim):
        super().__init__()

        self._atom_dim = dim

        self.activation = nn.Softplus(beta=0.5, threshold=14)

        self.node_layer1 = nn.Linear(dim, dim, bias=True)
        self.edge_layer1 = nn.Linear(dim, dim, bias=True)
        self.conv_layer = VEConv(rbf_dim, dim)
        self.node_layer2 = nn.Linear(dim, dim)
        self.node_layer3 = nn.Linear(dim, dim)

    def forward(self, g, level=1):
        g.ndata["new_node"] = self.node_layer1(g.ndata["node_%s" %
                                                       (level - 1)])
        node = self.conv_layer(g)
        g.edata["edge_f"] = self.activation(self.edge_layer1(
            g.edata["edge_f"]))
        node_1 = self.node_layer2(node)
        node_1a = self.activation(node_1)
        new_node = self.node_layer3(node_1a)

        g.ndata["node_%s" % (level)] = g.ndata["node_%s" %
                                               (level - 1)] + new_node

        return g.ndata["node_%s" % (level)]
