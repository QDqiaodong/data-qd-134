"""Full-solution classification checked against an exhaustive oracle.

Every graph up to 3x3 is enumerated exhaustively; larger graphs are random up
to 7x7, including alternating cycles, deficient chains, isolated vertices and
locally-multiple-but-globally-forced pairs.  A separate construction shows the
classifier never enumerates maximum matchings: a component with two perfect
matchings already has 2^(n/2) of them, and it is classified in linear time.
"""
import random,time,unittest
from venue_clearing.graph import classify,maximum_matching
from tests.vc_test_oracle import all_solutions

def check(testcase,left,right,adjacency):
    size,peers,can=classify(left,right,adjacency)
    expected_size,pairs,can_unmatched=all_solutions(left,right,adjacency)
    testcase.assertEqual(size,expected_size)
    for u in left:
        testcase.assertEqual(peers[('L',u)],{v for a,v in pairs if a==u})
        testcase.assertEqual(can[('L',u)],u in can_unmatched)
    for v in right:
        testcase.assertEqual(peers[('R',v)],{a for a,b in pairs if b==v})
        testcase.assertEqual(can[('R',v)],v in can_unmatched)

class Exhaustive(unittest.TestCase):
    def test_every_graph_up_to_3x3(self):
        for n_left in range(4):
            for n_right in range(4):
                left=['p%d'%i for i in range(n_left)]
                right=['c%d'%i for i in range(n_right)]
                pairs=[(u,v) for u in left for v in right]
                for mask in range(1<<len(pairs)):
                    adjacency={u:[] for u in left}
                    for bit,(u,v) in enumerate(pairs):
                        if mask>>bit&1:adjacency[u].append(v)
                    check(self,left,right,adjacency)

class Structured(unittest.TestCase):
    def test_alternating_cycle_makes_every_vertex_flexible(self):
        # A 4-cycle: every vertex has two possible peers and none can be
        # unmatched, because the graph has a perfect matching.
        left=['p1','p2'];right=['c1','c2']
        adjacency={'p1':['c1','c2'],'p2':['c1','c2']}
        size,peers,can=classify(left,right,adjacency)
        self.assertEqual(size,2)
        for u in left:
            self.assertEqual(peers[('L',u)],{'c1','c2'});self.assertFalse(can[('L',u)])
        for v in right:
            self.assertEqual(peers[('R',v)],{'p1','p2'});self.assertFalse(can[('R',v)])

    def test_deficient_chain(self):
        # p1-c1-p2-c2-p3: maximum size 2; any one of the three payments can
        # stay unmatched (p2 by pairing p1-c1 and p3-c2), and c1 can be free
        # when p2 takes c2 and p1 stays unmatched.
        left=['p1','p2','p3'];right=['c1','c2']
        adjacency={'p1':['c1'],'p2':['c1','c2'],'p3':['c2']}
        size,peers,can=classify(left,right,adjacency)
        self.assertEqual(size,2)
        self.assertTrue(can[('L','p1')] and can[('L','p2')] and can[('L','p3')])
        self.assertFalse(can[('R','c1')] or can[('R','c2')])
        self.assertEqual(peers[('L','p1')],{'c1'})
        self.assertEqual(peers[('R','c1')],{'p1','p2'})

    def test_isolated_vertices_are_unmatched(self):
        left=['p1','p2'];right=['c1']
        adjacency={'p1':['c1'],'p2':[]}
        size,peers,can=classify(left,right,adjacency)
        self.assertEqual(size,1)
        self.assertEqual(peers[('L','p2')],set())
        self.assertTrue(can[('L','p2')])
        self.assertFalse(can[('L','p1')])
        self.assertEqual(peers[('L','p1')],{'c1'})

    def test_locally_multiple_but_globally_forced(self):
        # p1 has two candidate credits; only c1 can serve p2, so p1 must take
        # c2 and c1 must take p2: both pairs are forced globally.
        left=['p1','p2'];right=['c1','c2']
        adjacency={'p1':['c1','c2'],'p2':['c1']}
        size,peers,can=classify(left,right,adjacency)
        self.assertEqual(size,2)
        self.assertEqual(peers[('L','p1')],{'c2'})
        self.assertEqual(peers[('L','p2')],{'c1'})
        self.assertFalse(can[('L','p1')] or can[('L','p2')])

    def test_random_graphs_up_to_7x7(self):
        random.seed(20261116)
        for _ in range(1500):
            n_left=random.randint(0,7);n_right=random.randint(0,7)
            left=['p%d'%i for i in range(n_left)]
            right=['c%d'%i for i in range(n_right)]
            density=random.choice([0.05,0.15,0.3,0.5,0.8,1.0])
            adjacency={u:[] for u in left}
            for u in left:
                for v in right:
                    if random.random()<density:adjacency[u].append(v)
            check(self,left,right,adjacency)

    def test_random_matching_size_matches_oracle(self):
        random.seed(99)
        for _ in range(500):
            n=random.randint(1,12)
            left=['p%d'%i for i in range(n)]
            right=['c%d'%i for i in range(max(0,n+random.randint(-2,2)))]
            adjacency={u:[] for u in left}
            for u in left:
                for v in right:
                    if random.random()<0.25:adjacency[u].append(v)
            _ml,_mr,size=maximum_matching(left,adjacency)
            expected,_,_=all_solutions(left,right,adjacency)
            self.assertEqual(size,expected)

class NoEnumeration(unittest.TestCase):
    def test_exponentially_many_maximum_matchings_in_linear_time(self):
        # A disjoint union of k 4-cycles has 2^k maximum matchings; k=400
        # means 2^400 solutions.  Enumeration is physically impossible, yet
        # classification is a single linear pass over the component.
        k=400
        left=[];right=[];adjacency={}
        for i in range(k):
            a,b='p%da'%i,'p%db'%i
            x,y='c%da'%i,'c%db'%i
            left.extend([a,b]);right.extend([x,y])
            adjacency[a]=[x,y];adjacency[b]=[x,y]
        started=time.monotonic()
        size,peers,can=classify(left,right,adjacency)
        elapsed=time.monotonic()-started
        self.assertEqual(size,2*k)
        self.assertEqual(len(peers[('L',left[0])]),2)
        self.assertFalse(can[('L',left[0])])
        self.assertLess(elapsed,5.0)

    def test_long_path_component(self):
        # 4000 vertices in one connected component (the contract's limit),
        # shaped so augmenting paths are long.
        n=2000
        left=['p%d'%i for i in range(n)];right=['c%d'%i for i in range(n)]
        adjacency={left[i]:([right[0]] if i==0 else [right[i-1],right[i]]) for i in range(n)}
        started=time.monotonic()
        size,peers,can=classify(left,right,adjacency)
        elapsed=time.monotonic()-started
        self.assertEqual(size,n)
        self.assertLess(elapsed,10.0)

if __name__=='__main__':unittest.main()
