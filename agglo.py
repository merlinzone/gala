
from itertools import combinations, izip

from heapq import heapify, heappush, heappop
from numpy import array, mean, zeros, zeros_like, uint8, int8, where, unique, \
    finfo, float, size, double, transpose, newaxis
from scipy.stats import sem
from networkx import Graph
from networkx.algorithms.traversal.depth_first_search import dfs_preorder_nodes
import morpho
import iterprogress as ip
from mergequeue import MergeQueue

class Rag(Graph):
    """Region adjacency graph for segmentation of nD volumes."""

    def __init__(self, watershed=None, probabilities=None, 
            merge_priority_function=None, show_progress=False, lowmem=False):
        """Create a graph from a watershed volume and image volume.
        
        The watershed is assumed to have dams of label 0 in between basins.
        Then, each basin corresponds to a node in the graph and an edge is
        placed between two nodes if there are one or more watershed pixels
        connected to both corresponding basins.
        """
        super(Rag, self).__init__(weighted=False)
        self.boundary_probability = 10**100 #inconceivably high, but no overflow
        if probabilities is not None:
            self.set_probabilities(probabilities)
        self.show_progress = show_progress
        if merge_priority_function is None:
            self.merge_priority_function = boundary_mean
        else:
            self.merge_priority_function = merge_priority_function
        if watershed is not None:
            self.set_watershed(watershed, lowmem)
            self.build_graph_from_watershed()
        self.merge_queue = MergeQueue()

    def build_graph_from_watershed(self):
        zero_idxs = where(self.watershed.ravel() == 0)[0]
        if self.show_progress:
            def with_progress(seq, length=None, title='Progress: '):
                return ip.with_progress(seq, length, title,
                                                ip.StandardProgressBar())
        else:
            def with_progress(seq, length=None, title='Progress: '):
                return ip.with_progress(seq, length, title, ip.NoProgressBar())
        for idx in with_progress(zero_idxs, title='Building edges... '):
            ns = self.neighbor_idxs(idx)
            adj_labels = self.watershed.ravel()[ns]
            adj_labels = unique(adj_labels[adj_labels != 0])
            for l1,l2 in combinations(adj_labels, 2):
                if self.has_edge(l1, l2): 
                    self[l1][l2]['boundary'].add(idx)
                else: 
                    self.add_edge(l1, l2, boundary=set([idx]))
        nonzero_idxs = where(self.watershed.ravel() != 0)[0]
        if not hasattr(self, 'probabilities'):
            self.probabilities = zeros(self.watershed.shape, uint8)
        for idx in with_progress(nonzero_idxs, title='Building nodes... '):
            nodeid = self.watershed.ravel()[idx]
            p = self.probabilities.ravel()[idx]
            try:
                self.node[nodeid]['extent'].add(idx)
                self.node[nodeid]['sump'] += p
                self.node[nodeid]['sump2'] += p*p
            except KeyError:
                self.node[nodeid]['extent'] = set([idx])
                self.node[nodeid]['sump'] = double(p)
                self.node[nodeid]['sump2'] = double(p*p)

    def get_neighbor_idxs_fast(self, idxs):
        return self.pixel_neighbors[idxs]

    def get_neighbor_idxs_lean(self, idxs):
        return morpho.get_neighbor_idxs(self.watershed, idxs)

    def set_probabilities(self, probs):
        self.probabilities = morpho.pad(probs, [self.boundary_probability, 0])

    def set_watershed(self, ws, lowmem=False):
        self.boundary_body = ws.max()+1
        self.watershed = morpho.pad(ws, [0, self.boundary_body])
        self.segmentation = self.watershed.copy()
        if lowmem:
            self.neighbor_idxs = self.get_neighbor_idxs_lean
        else:
            self.pixel_neighbors = morpho.build_neighbors_array(self.watershed)
            self.neighbor_idxs = self.get_neighbor_idxs_fast

    def build_merge_queue(self):
        """Build a queue of node pairs to be merged in a specific priority.
        
        The queue elements have a specific format in order to allow 'removing'
        of specific elements inside the priority queue. Each element is a list
        of length 4 containing:
            - the merge priority (any ordered type)
            - a 'valid' flag
            - and the two nodes in arbitrary order
        The valid flag allows one to "remove" elements by setting the flag to
        False. Then one checks the flag when popping elements and ignores those
        marked as invalid.

        One other specific feature is that there are back-links from edges to
        their corresponding queue items so that when nodes are merged,
        affected edges can be invalidated and reinserted in the queue.
        """
        queue_items = []
        for l1, l2 in self.edges_iter():
            qitem = [self.merge_priority_function(self,l1,l2), True, l1, l2]
            queue_items.append(qitem)
            self[l1][l2]['qlink'] = qitem
        return MergeQueue(queue_items, with_progress=self.show_progress)

    def rebuild_merge_queue(self):
        """Build a merge queue from scratch and assign to self.merge_queue."""
        self.merge_queue = self.build_merge_queue()

    def agglomerate(self, threshold=128, generate=False):
        """Merge nodes sequentially until given edge confidence threshold."""
        if self.merge_queue.is_empty():
            self.merge_queue = self.build_merge_queue()
        while len(self.merge_queue) > 0 and \
                                        self.merge_queue.peek()[0] < threshold:
            merge_priority, valid, n1, n2 = self.merge_queue.pop()
            if valid:
                self.merge_nodes(n1,n2)
                if generate:
                    yield n1, n2

    def agglomerate_ladder(self, threshold=1000, strictness=1):
        """Merge sequentially all nodes smaller than threshold.
        
        Note: nodes that are on the volume boundary are not agglomerated.
        """
        original_merge_priority_function = self.merge_priority_function
        self.merge_priority_function = make_ladder(
            self.merge_priority_function, threshold, strictness
        )
        self.rebuild_merge_queue()
        self.agglomerate(self.boundary_probability/10)
        self.merge_priority_function = original_merge_priority_function
        self.merge_queue.finish()

    def merge_nodes(self, n1, n2):
        """Merge two nodes, while updating the necessary edges."""
        new_neighbors = [n for n in self.neighbors(n2) if n != n1]
        for n in new_neighbors:
            if self.has_edge(n, n1):
                self[n1][n]['boundary'].update(self[n2][n]['boundary'])
            else:
                self.add_edge(n, n1, boundary=self[n2][n]['boundary'])
        for n in self.neighbors(n2):
            if n != n1:
                self.merge_edge_properties((n2,n), (n1,n))
        self.node[n1]['extent'].update(self.node[n2]['extent'])
        self.node[n1]['sump'] += self.node[n2]['sump']
        self.node[n1]['sump2'] += self.node[n2]['sump2']
        self.segmentation.ravel()[list(self.node[n2]['extent'])] = n1
        if self.has_edge(n1,n2):
            boundary = array(list(self[n1][n2]['boundary']))
            boundary_neighbor_pixels = self.segmentation.ravel()[
                self.neighbor_idxs(boundary)
            ]
            add = ( (boundary_neighbor_pixels == 0) + 
                (boundary_neighbor_pixels == n1) + 
                (boundary_neighbor_pixels == n2) ).all(axis=1)
            check = True-add
            self.node[n1]['extent'].update(boundary[add])
            boundary_probs = self.probabilities.ravel()[boundary[add]]
            self.node[n1]['sump'] += boundary_probs.sum()
            self.node[n1]['sump2'] += (boundary_probs*boundary_probs).sum()
            self.segmentation.ravel()[boundary[add]] = n1
            boundaries_to_edit = {}
            for px in boundary[check]:
                for lb in unique(
                            self.segmentation.ravel()[self.neighbor_idxs(px)]):
                    if lb != n1 and lb != 0:
                        try:
                            boundaries_to_edit[(n1,lb)].append(px)
                        except KeyError:
                            boundaries_to_edit[(n1,lb)] = [px]
            for u, v in boundaries_to_edit.keys():
                if self.has_edge(u, v):
                    self[u][v]['boundary'].update(boundaries_to_edit[(u,v)])
                else:
                    self.add_edge(u, v, boundary=set(boundaries_to_edit[(u,v)]))
                self.update_merge_queue(u, v)
            for n in new_neighbors:
                if not boundaries_to_edit.has_key((n1,n)):
                    self.update_merge_queue(n1, n)
        self.remove_node(n2)

    def merge_edge_properties(self, src, dst):
        """Merge the properties of edge src into edge dst."""
        u, v = dst
        w, x = src
        if not self.has_edge(u,v):
            self.add_edge(u, v, boundary=self[w][x]['boundary'])
        else:
            self[u][v]['boundary'].update(self[w][x]['boundary'])
        try:
            self.merge_queue.invalidate(self[w][x]['qlink'])
            self.update_merge_queue(u, v)
        except KeyError:
            pass

    def update_merge_queue(self, u, v):
        """Update the merge queue item for edge (u,v). Add new by default."""
        if self[u][v].has_key('qlink'):
            self.merge_queue.invalidate(self[u][v]['qlink'])
        if not self.merge_queue.is_null_queue:
            new_qitem = [self.merge_priority_function(self,u,v), True, u, v]
            self[u][v]['qlink'] = new_qitem
            self.merge_queue.push(new_qitem)

    def get_segmentation(self):
        return morpho.juicy_center(self.segmentation, 2)

    def build_volume(self):
        """Return the segmentation (numpy.ndarray) induced by the graph."""
        v = zeros_like(self.watershed)
        for n in self.nodes():
            v.ravel()[list(self.node[n]['extent'])] = n
        return morpho.juicy_center(v,2)

    def at_volume_boundary(self, n):
        """Return True if node n touches the volume boundary."""
        return self.has_edge(n, self.boundary_body)

    def write(self, fout, format='GraphML'):
        pass

############################
# Merge priority functions #
############################

def boundary_mean(g, n1, n2):
    return mean(g.probabilities.ravel()[list(g[n1][n2]['boundary'])])

def make_ladder(priority_function, threshold, strictness=1):
    def ladder_function(g, n1, n2):
        s1 = len(g.node[n1]['extent'])
        s2 = len(g.node[n2]['extent'])
        ladder_condition = \
                (s1 < threshold and not g.at_volume_boundary(n1)) or \
                (s2 < threshold and not g.at_volume_boundary(n2))
        if strictness >= 2:
            ladder_condition &= ((s1 < threshold) != (s2 < threshold))
        if strictness >= 3:
            ladder_condition &= len(g[n1][n2]['boundary']) > 2

        if ladder_condition:
            return priority_function(g, n1, n2)
        else:
            return finfo(float).max / size(g.segmentation)
    return ladder_function

def classifier_probability(feature_extractor, classifier):
    def predict(g, n1, n2):
        features = feature_extractor(g, n1, n2)
        try:
            prediction = classifier.predict_proba(features)[0,1]
        except AttributeError:
            prediction = classifier.predict(features)[0]
        return prediction
    return predict

def boundary_mean_ladder(g, n1, n2, threshold, strictness=1):
    f = make_ladder(boundary_mean, threshold, strictness)
    return f(g, n1, n2)

def boundary_mean_plus_sem(g, n1, n2, alpha=-6):
    bvals = g.probabilities.ravel()[list(g[n1][n2]['boundary'])]
    return mean(bvals) + alpha*sem(bvals)

# RUG #

class Rug(object):
    """Region union graph, used to compare two segmentations."""
    def __init__(self, s1=None, s2=None, progress=False):
        self.s1 = s1
        self.s2 = s2
        self.progress = progress
        if s1 is not None and s2 is not None:
            self.build_graph(s1, s2)

    def build_graph(self, s1, s2):
        if s1.shape != s2.shape:
            raise RuntimeError('Error building region union graph: '+
                'volume shapes don\'t match. '+str(s1.shape)+' '+str(s2.shape))
        n1 = len(unique(s1))
        n2 = len(unique(s2))
        self.overlaps = zeros((n1,n2), double)
        self.sizes1 = zeros(n1, double)
        self.sizes2 = zeros(n2, double)
        if self.progress:
            def with_progress(seq):
                return ip.with_progress(seq, length=s1.size,
                            title='RUG...', pbar=ip.StandardProgressBar())
        else:
            def with_progress(seq): return seq
        for v1, v2 in with_progress(izip(s1.ravel(), s2.ravel())):
            self.overlaps[v1,v2] += 1
            self.sizes1[v1] += 1
            self.sizes2[v2] += 1
        self.overlaps[:,0] = 0
        self.overlaps[0,:] = 0
        self.overlaps[0,0] = 1

    def __getitem__(self, v):
        try:
            l = len(v)
        except TypeError:
            v = [v]
            l = 1
        v1 = v[0]
        v2 = Ellipsis
        do_transpose = False
        if l >= 2:
            v2 = v[1]
        if l >= 3:
            do_transpose = bool(v[2])
        if do_transpose:
            return transpose(self.overlaps)[v1,v2]/self.sizes2[v1,newaxis]
        else:
            return self.overlaps[v1,v2]/self.sizes1[v1,newaxis]

def best_possible_segmentation(ws, gt):
    """Build the best possible segmentation given a superpixel map."""
    ws = Rag(ws)
    gt = Rag(gt)
    rug = Rug(ws.get_segmentation(), gt.get_segmentation())
    assignment = rug.overlaps == rug.overlaps.max(axis=1)[:,newaxis]
    hard_assignment = where(assignment.sum(axis=1) > 1)[0]
    # currently ignoring hard assignment nodes
    assignment[hard_assignment,:] = 0
    for gt_node in range(1,len(rug.sizes2)):
        sp_subgraph = ws.subgraph(where(assignment[:,gt_node])[0])
        if len(sp_subgraph) > 0:
            sp_dfs = list(dfs_preorder_nodes(sp_subgraph)) 
                    # dfs_preorder_nodes returns iter, convert to list
            source_sp, other_sps = sp_dfs[0], sp_dfs[1:]
            for current_sp in other_sps:
                ws.merge_nodes(source_sp, current_sp)
    return ws.get_segmentation()
