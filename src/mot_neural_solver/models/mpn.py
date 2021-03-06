import torch
import copy
import time
from torch import nn

from torch_scatter import scatter_mean, scatter_max, scatter_add

from mot_neural_solver.models.mlp import MLP

def scatter_add_weigh(src: torch.Tensor, index: torch.Tensor, dim: int = 0,
                      dim_size: int = None,
                      weigh: torch.Tensor = None) -> torch.Tensor:
    """according index, add src * weigh"""
    size = list(src.size())
    if dim_size is not None:
        size[dim] = dim_size
    elif index.numel() == 0:
        size[dim] = 0
    else:
        size[dim] = int(index.max()) + 1
    out = torch.zeros(size, dtype=src.dtype, device=src.device)
    if dim == 0:
        for i in range(src.size()[0]):
            out[index[i]][:] += weigh[i] * src[i][:]
    else:
        raise ValueError("Sorry, I just code a dim")
    return out

class MetaLayer(torch.nn.Module):
    """
    Core Message Passing Network Class. Extracted from torch_geometric, with minor modifications.
    (https://rusty1s.github.io/pytorch_geometric/build/html/modules/nn.html)
    """
    def __init__(self, edge_model=None, node_model=None):
        """
        Args:
            edge_model: Callable Edge Update Model
            node_model: Callable Node Update Model
        """
        super(MetaLayer, self).__init__()

        self.edge_model = edge_model
        self.node_model = node_model
        self.reset_parameters()

    def reset_parameters(self):
        for item in [self.node_model, self.edge_model]:
            if hasattr(item, 'reset_parameters'):
                item.reset_parameters()

    def forward(self, x, edge_index, edge_attr):
        """
        Does a single node and edge feature vectors update.
        Args:
            x: node features matrix
            edge_index: tensor with shape [2, M], with M being the number of edges, indicating nonzero entries in the
            graph adjacency (i.e. edges)
            edge_attr: edge features matrix (ordered by edge_index)

        Returns: Updated Node and Edge Feature matrices

        """
        row, col = edge_index

        # Edge Update
        if self.edge_model is not None:
            edge_attr = self.edge_model(x[row], x[col], edge_attr)

        # Node Update
        if self.node_model is not None:
            x = self.node_model(x, edge_index, edge_attr)

        return x, edge_attr

    def __repr__(self):
        return '{}(edge_model={}, node_model={})'.format(self.__class__.__name__, self.edge_model, self.node_model)

class EdgeModel(nn.Module):
    """
    Class used to peform the edge update during Neural message passing
    """
    def __init__(self, edge_mlp):
        super(EdgeModel, self).__init__()
        self.edge_mlp = edge_mlp

    def forward(self, source, target, edge_attr):
        out = torch.cat([source, target, edge_attr], dim=1)
        return self.edge_mlp(out)

class TimeAwareNodeModel(nn.Module):
    """
    Class used to peform the node update during Neural mwssage passing
    """
    def __init__(self, flow_in_mlp, flow_out_mlp, node_mlp, node_agg_fn):
        super(TimeAwareNodeModel, self).__init__()

        self.flow_in_mlp = flow_in_mlp
        self.flow_out_mlp = flow_out_mlp
        self.node_mlp = node_mlp
        self.node_agg_fn = node_agg_fn

    def forward(self, x, edge_index, edge_attr):
        row, col = edge_index
        flow_out_mask = row < col
        flow_out_row, flow_out_col = row[flow_out_mask], col[flow_out_mask]
        flow_out_input = torch.cat([x[flow_out_col], edge_attr[flow_out_mask]], dim=1)
        flow_out = self.flow_out_mlp(flow_out_input)
        # flow_out = self.node_agg_fn(flow_out, flow_out_row, x.size(0))

        flow_in_mask = row > col
        flow_in_row, flow_in_col = row[flow_in_mask], col[flow_in_mask]
        flow_in_input = torch.cat([x[flow_in_col], edge_attr[flow_in_mask]], dim=1)
        flow_in = self.flow_in_mlp(flow_in_input)

        # flow_in = self.node_agg_fn(flow_in, flow_in_row, x.size(0))
        # flow = torch.cat((flow_in, flow_out), dim=1)

        return {'flow_out':flow_out, 'flow_in':flow_in,  'x_size0': x.size(0)}
        # return self.node_mlp(flow)

class MLPGraphIndependent(nn.Module):
    """
    Class used to to encode (resp. classify) features before (resp. after) neural message passing.
    It consists of two MLPs, one for nodes and one for edges, and they are applied independently to node and edge
    features, respectively.

    This class is based on: https://github.com/deepmind/graph_nets tensorflow implementation.
    """

    def __init__(self, edge_in_dim = None, node_in_dim = None, edge_out_dim = None, node_out_dim = None,
                 node_fc_dims = None, edge_fc_dims = None, dropout_p = None, use_batchnorm = None):
        super(MLPGraphIndependent, self).__init__()

        if node_in_dim is not None :
            self.node_mlp = MLP(input_dim=node_in_dim, fc_dims=list(node_fc_dims) + [node_out_dim],
                                dropout_p=dropout_p, use_batchnorm=use_batchnorm)
        else:
            self.node_mlp = None

        if edge_in_dim is not None :
            self.edge_mlp = MLP(input_dim=edge_in_dim, fc_dims=list(edge_fc_dims) + [edge_out_dim],
                                dropout_p=dropout_p, use_batchnorm=use_batchnorm)
        else:
            self.edge_mlp = None

    def forward(self, edge_feats = None, nodes_feats = None):

        if self.node_mlp is not None:
            out_node_feats = self.node_mlp(nodes_feats)

        else:
            out_node_feats = nodes_feats

        if self.edge_mlp is not None:
            out_edge_feats = self.edge_mlp(edge_feats)

        else:
            out_edge_feats = edge_feats

        return out_edge_feats, out_node_feats

class MOTMPNet(nn.Module):
    """
    Main Model Class. Contains all the components of the model. It consists of of several networks:
    - 2 encoder MLPs (1 for nodes, 1 for edges) that provide the initial node and edge embeddings, respectively,
    - 4 update MLPs (3 for nodes, 1 per edges used in the 'core' Message Passing Network
    - 1 edge classifier MLP that performs binary classification over the Message Passing Network's output.

    This class was initially based on: https://github.com/deepmind/graph_nets tensorflow implementation.
    """

    def __init__(self, model_params, bb_encoder = None):
        """
        Defines all components of the model
        Args:
            bb_encoder: (might be 'None') CNN used to encode bounding box apperance information.
            model_params: dictionary contaning all model hyperparameters
        """
        super(MOTMPNet, self).__init__()

        self.node_cnn = bb_encoder
        self.model_params = model_params

        # Define Encoder and Classifier Networks
        encoder_feats_dict = model_params['encoder_feats_dict']
        classifier_feats_dict = model_params['classifier_feats_dict']
        edge_merge_multiply_dict = model_params['edge_merge_multiply_dict']
        node_merge_multiply_dict = model_params['node_merge_multiply_dict']

        self.encoder = MLPGraphIndependent(**encoder_feats_dict)
        self.classifier = MLPGraphIndependent(**classifier_feats_dict)
        # self.edge_merge_fc = MLP(input_dim=edge_merge_multiply_dict['in_dim'],
        #                          fc_dims=edge_merge_multiply_dict['out_dim'])
        # self.node_merge_fc = MLP(input_dim=node_merge_multiply_dict['in_dim'],
        #                          fc_dims=node_merge_multiply_dict['out_dim'])


        # Define the 'Core' message passing network (i.e. node and edge update models)
        self.MPNet = self._build_core_MPNet(model_params=model_params, encoder_feats_dict=encoder_feats_dict)
        # self.MPNet_v2 = self._build_core_MPNet(model_params=model_params, encoder_feats_dict=encoder_feats_dict)
        # self.MPNet_v3 = self._build_core_MPNet(model_params=model_params, encoder_feats_dict=encoder_feats_dict)


        self.num_enc_steps = model_params['num_enc_steps']
        self.num_class_steps = model_params['num_class_steps']


    def _build_core_MPNet(self, model_params, encoder_feats_dict):
        """
        Builds the core part of the Message Passing Network: Node Update and Edge Update models.
        Args:
            model_params: dictionary contaning all model hyperparameters
            encoder_feats_dict: dictionary containing the hyperparameters for the initial node/edge encoder
        """

        # Define an aggregation operator for nodes to 'gather' messages from incident edges
        node_agg_fn = model_params['node_agg_fn']
        assert node_agg_fn.lower() in ('mean', 'max', 'sum'), "node_agg_fn can only be 'max', 'mean' or 'sum'."

        if node_agg_fn == 'mean':
            node_agg_fn = lambda out, row, x_size: scatter_mean(out, row, dim=0, dim_size=x_size)

        elif node_agg_fn == 'max':
            node_agg_fn = lambda out, row, x_size: scatter_max(out, row, dim=0, dim_size=x_size)[0]

        elif node_agg_fn == 'sum':
            node_agg_fn = lambda out, row, x_size: scatter_add(out, row, dim=0, dim_size=x_size)
            # node_agg_fn = lambda out, row, x_size, weigh: scatter_add_weigh(out, row, dim=0, dim_size=x_size, weigh=weigh)

        self.node_agg_fn = node_agg_fn

        # Define all MLPs involved in the graph network
        # For both nodes and edges, the initial encoded features (i.e. output of self.encoder) can either be
        # reattached or not after each Message Passing Step. This affects MLPs input dimensions
        self.reattach_initial_nodes = model_params['reattach_initial_nodes']
        self.reattach_initial_edges = model_params['reattach_initial_edges']

        edge_factor = 2 if self.reattach_initial_edges else 1
        node_factor = 2 if self.reattach_initial_nodes else 1

        edge_model_in_dim = node_factor * 2 * encoder_feats_dict['node_out_dim'] + edge_factor * encoder_feats_dict[
            'edge_out_dim']
        node_model_in_dim = node_factor * encoder_feats_dict['node_out_dim'] + encoder_feats_dict['edge_out_dim']

        # Define all MLPs used within the MPN
        edge_model_feats_dict = model_params['edge_model_feats_dict']
        node_model_feats_dict = model_params['node_model_feats_dict']

        edge_mlp = MLP(input_dim=edge_model_in_dim,
                       fc_dims=edge_model_feats_dict['fc_dims'], #[80, 16]
                       dropout_p=edge_model_feats_dict['dropout_p'],
                       use_batchnorm=edge_model_feats_dict['use_batchnorm'])

        flow_in_mlp = MLP(input_dim=node_model_in_dim,
                          fc_dims=node_model_feats_dict['fc_dims'], #[56, 32]
                          dropout_p=node_model_feats_dict['dropout_p'],
                          use_batchnorm=node_model_feats_dict['use_batchnorm'])

        flow_out_mlp = MLP(input_dim=node_model_in_dim,
                           fc_dims=node_model_feats_dict['fc_dims'], #[56, 32]
                           dropout_p=node_model_feats_dict['dropout_p'],
                           use_batchnorm=node_model_feats_dict['use_batchnorm'])

        node_mlp = nn.Sequential(*[nn.Linear(2 * encoder_feats_dict['node_out_dim'],
                                             encoder_feats_dict['node_out_dim']),
                                   nn.ReLU(inplace=True)])

        self.node_mlp = node_mlp

        # Define all MLPs used within the MPN
        return MetaLayer(edge_model=EdgeModel(edge_mlp = edge_mlp),
                         node_model=TimeAwareNodeModel(flow_in_mlp = flow_in_mlp,
                                                       flow_out_mlp = flow_out_mlp,
                                                       node_mlp = node_mlp,
                                                       node_agg_fn = node_agg_fn))


    def forward(self, data):
        """
        Provides a fractional solution to the data association problem.
        First, node and edge features are independently encoded by the encoder network. Then, they are iteratively
        'combined' for a fixed number of steps via the Message Passing Network (self.MPNet). Finally, they are
        classified independently by the classifiernetwork.
        Args:
            data: object containing attribues
              - x: node features matrix
              - edge_index: tensor with shape [2, M], with M being the number of edges, indicating nonzero entries in the
                graph adjacency (i.e. edges) (i.e. sparse adjacency)
              - edge_attr: edge features matrix (sorted by edge apperance in edge_index)

        Returns:
            classified_edges: list of unnormalized node probabilites after each MP step
        """
        x, edge_index, edge_attr = data.x, data.edge_index, data.edge_attr

        x_is_img = len(x.shape) == 4
        if self.node_cnn is not None and x_is_img:
            x = self.node_cnn(x)

            emb_dists = nn.functional.pairwise_distance(x[edge_index[0]], x[edge_index[1]]).view(-1, 1)
            edge_attr = torch.cat((edge_attr, emb_dists), dim = 1)

        # Encoding features step
        latent_edge_feats, latent_node_feats = self.encoder(edge_attr, x)
        initial_edge_feats = latent_edge_feats
        initial_node_feats = latent_node_feats

        # During training, the feature vectors that the MPNetwork outputs for the  last self.num_class_steps message
        # passing steps are classified in order to compute the loss.
        first_class_step = self.num_enc_steps - self.num_class_steps + 1
        outputs_dict = {'classified_edges': [], 'node_feats':[]}
        outputs_dict['node_feats'].append(latent_node_feats)
        for step in range(1, self.num_enc_steps + 1):

            # Reattach the initially encoded embeddings before the update
            if self.reattach_initial_edges:
                latent_edge_feats = torch.cat((initial_edge_feats, latent_edge_feats), dim=1)
            if self.reattach_initial_nodes:
                latent_node_feats = torch.cat((initial_node_feats, latent_node_feats), dim=1)

            # Message Passing Step
            # latent_node_feats_v1, latent_edge_feats_v1 = self.MPNet(latent_node_feats, edge_index, latent_edge_feats)
            # latent_node_feats_v2, latent_edge_feats_v2 = self.MPNet_v2(latent_node_feats, edge_index, latent_edge_feats)
            # latent_node_feats_v3, latent_edge_feats_v3 = self.MPNet_v3(latent_node_feats, edge_index, latent_edge_feats)

            #Concatenate multiply MPN
            # latent_node_feats = torch.cat([latent_node_feats_v1, latent_node_feats_v2, latent_node_feats_v3], dim=1)
            # latent_edge_feats = torch.cat([latent_edge_feats_v1, latent_edge_feats_v2, latent_edge_feats_v3], dim=1)
            # latent_node_feats = self.node_merge_fc(latent_node_feats)
            # latent_edge_feats = self.edge_merge_fc(latent_edge_feats)

            #Weighted edge

            node_feats_set, latent_edge_feats = self.MPNet(latent_node_feats, edge_index, latent_edge_feats)

            if step >= first_class_step:
                # Classification Step
                dec_edge_feats, _ = self.classifier(latent_edge_feats)
                outputs_dict['classified_edges'].append(dec_edge_feats)
                dec_edge_weigh = nn.Sigmoid()(dec_edge_feats.reshape(-1))

                # when there is edge prediction, update node_feature
                x_size0 = node_feats_set['x_size0']
                row, col = edge_index
                flow_out_mask = row < col
                flow_out_row, flow_out_col = row[flow_out_mask], col[flow_out_mask]
                flow_out_edge_weigh = dec_edge_weigh[flow_out_mask]
                flow_out = node_feats_set['flow_out']
                flow_out = self.node_agg_fn(flow_out * flow_out_edge_weigh.reshape(-1, 1), flow_out_row, x_size0)

                flow_in_mask = row > col
                flow_in_row, flow_in_col = row[flow_in_mask], col[flow_in_mask]
                flow_in_edge_weigh = dec_edge_weigh[flow_in_mask]
                flow_in = node_feats_set['flow_in']
                flow_in = self.node_agg_fn(flow_in * flow_in_edge_weigh.reshape(-1, 1), flow_in_row, x_size0)

                flow = torch.cat((flow_in, flow_out), dim=1)

                flow = torch.cat((flow_in, flow_out), dim=1)
                latent_node_feats = self.node_mlp(flow)
                outputs_dict['node_feats'].append(latent_node_feats)


            else:
                #when no edge prediction, update node_feature
                x_size0 = node_feats_set['x_size0']
                row, col = edge_index
                flow_out_mask = row < col
                flow_out_row, flow_out_col = row[flow_out_mask], col[flow_out_mask]
                flow_out = node_feats_set['flow_out']
                # flow_out = self.node_agg_fn(flow_out, flow_out_row, x_size0, torch.ones(flow_out.size()[0]).cuda())
                flow_out = self.node_agg_fn(flow_out, flow_out_row, x_size0)


                flow_in_mask = row > col
                flow_in_row, flow_in_col = row[flow_in_mask], col[flow_in_mask]
                flow_in = node_feats_set['flow_in']
                # flow_in = self.node_agg_fn(flow_in, flow_in_row, x_size0, torch.ones(flow_in.size()[0]).cuda())
                flow_in = self.node_agg_fn(flow_in, flow_in_row, x_size0)

                flow = torch.cat((flow_in, flow_out), dim=1)

                flow = torch.cat((flow_in, flow_out), dim=1)
                latent_node_feats = self.node_mlp(flow)

        if self.num_enc_steps == 0:
            dec_edge_feats, _ = self.classifier(latent_edge_feats)
            outputs_dict['classified_edges'].append(dec_edge_feats)

        return outputs_dict