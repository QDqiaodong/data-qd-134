"""Bipartite matching and full-solution classification.

The expensive object here is not a maximum matching but the set of *all*
maximum matchings: the report must say, for every vertex, which peers can be
paired with it and whether it can stay unmatched in some maximum matching.
That set is exponential in general, so it is never enumerated.  Instead one
maximum matching M is computed, and a directed auxiliary graph D is built by
orienting every matched edge from its right endpoint to its left endpoint and
every unmatched edge from left to right:

* an edge belongs to some maximum matching iff it is in M, or its left
  endpoint is reachable from an unmatched left vertex in D, or its right
  endpoint can reach an unmatched right vertex in D, or both endpoints lie in
  the same strongly connected component of D;
* a left vertex can be unmatched iff it is reachable from an unmatched left
  vertex; a right vertex can be unmatched iff it reaches an unmatched right
  vertex.

These are the classical Gallai-Edmonds-style alternating-path conditions for
bipartite graphs.  The implementation below was checked against an exhaustive
oracle on every graph up to 3x3 and on tens of thousands of random graphs up
to 7x7 (see tests/vc_test_oracle.py).
"""
from collections import deque

def maximum_matching(left,adjacency):
    """Hopcroft-Karp.  Returns (match_left, match_right, size).

    `adjacency` maps each left vertex to a sequence of right vertices; the
    sequences are de-duplicated by the caller.  Everything is iterative so a
    4000-vertex component cannot hit the recursion limit.
    """
    match_left={};match_right={}
    infinity=float('inf')
    while True:
        distance={}
        queue=deque()
        for u in left:
            if u not in match_left:distance[u]=0;queue.append(u)
            else:distance[u]=infinity
        shortest=infinity
        while queue:
            u=queue.popleft()
            if distance[u]>=shortest:continue
            for v in adjacency.get(u,()):
                w=match_right.get(v)
                if w is None:shortest=distance[u]+1
                elif distance[w]==infinity:
                    distance[w]=distance[u]+1;queue.append(w)
        if shortest==infinity:break
        augmented=0
        for u in left:
            if u not in match_left:
                if _augment(u,adjacency,match_left,match_right,distance,shortest):
                    augmented+=1
        if augmented==0:break        # defensive: a phase must make progress
    return match_left,match_right,len(match_left)

def _augment(root,adjacency,match_left,match_right,distance,shortest):
    # Iterative layered DFS restricted to shortest augmenting paths.
    stack=[[root,iter(adjacency.get(root,()))]]
    path=[]
    while stack:
        node,neighbours=stack[-1]
        advanced=False
        for v in neighbours:
            w=match_right.get(v)
            if w is None:
                if distance[node]+1==shortest:
                    path.append((node,v))
                    for a,b in path:match_left[a]=b;match_right[b]=a
                    return True
                continue
            if distance.get(w,float('inf'))==distance[node]+1:
                path.append((node,v))
                stack.append([w,iter(adjacency.get(w,()))])
                advanced=True
                break
        if not advanced:
            distance[node]=float('inf')       # dead end: never revisit in this phase
            stack.pop()
            if path:path.pop()
    return False

def classify(left,right,adjacency):
    """Classify a bipartite graph by its entire maximum-matching solution set.

    Returns (size, peers, can_unmatched) where peers[u] is the set of right
    vertices that can be matched to u in some maximum matching (and vice
    versa), and can_unmatched[u] says whether u can be left unmatched in some
    maximum matching.
    """
    match_left,match_right,size=maximum_matching(left,adjacency)
    index={}
    for u in left:index[('L',u)]=len(index)
    for v in right:index[('R',v)]=len(index)
    n=len(index)
    forward=[[] for _ in range(n)];reverse=[[] for _ in range(n)]
    for u in left:
        lu=index[('L',u)]
        for v in adjacency.get(u,()):
            rv=index[('R',v)]
            if match_left.get(u)==v:forward[rv].append(lu);reverse[lu].append(rv)
            else:forward[lu].append(rv);reverse[rv].append(lu)
    # X: left vertices reachable from a free left vertex.
    in_x=[False]*n
    queue=deque()
    for u in left:
        if u not in match_left:
            i=index[('L',u)];in_x[i]=True;queue.append(i)
    while queue:
        node=queue.popleft()
        for w in forward[node]:
            if not in_x[w]:in_x[w]=True;queue.append(w)
    # Y: right vertices that can reach a free right vertex.
    in_y=[False]*n
    queue=deque()
    for v in right:
        if v not in match_right:
            i=index[('R',v)];in_y[i]=True;queue.append(i)
    while queue:
        node=queue.popleft()
        for w in reverse[node]:
            if not in_y[w]:in_y[w]=True;queue.append(w)
    component=_strong_components(forward,reverse)
    peers={}
    for u in left:peers[('L',u)]=set()
    for v in right:peers[('R',v)]=set()
    for u in left:
        lu=index[('L',u)]
        for v in adjacency.get(u,()):
            rv=index[('R',v)]
            allowed=(match_left.get(u)==v or in_x[lu] or in_y[rv]
                     or component[lu]==component[rv])
            if allowed:peers[('L',u)].add(v);peers[('R',v)].add(u)
    can_unmatched={}
    for u in left:can_unmatched[('L',u)]=in_x[index[('L',u)]]
    for v in right:can_unmatched[('R',v)]=in_y[index[('R',v)]]
    return size,peers,can_unmatched

def _strong_components(forward,reverse):
    """Iterative Kosaraju; returns a component id per node index."""
    n=len(forward)
    seen=[False]*n;order=[]
    for start in range(n):
        if seen[start]:continue
        seen[start]=True
        stack=[[start,0]]
        while stack:
            frame=stack[-1];node=frame[0];neighbours=forward[node]
            if frame[1]<len(neighbours):
                w=neighbours[frame[1]];frame[1]+=1
                if not seen[w]:seen[w]=True;stack.append([w,0])
            else:
                order.append(node);stack.pop()
    component=[-1]*n
    identifier=0
    for start in reversed(order):
        if component[start]!=-1:continue
        component[start]=identifier;stack=[start]
        while stack:
            node=stack.pop()
            for w in reverse[node]:
                if component[w]==-1:component[w]=identifier;stack.append(w)
        identifier+=1
    return component
