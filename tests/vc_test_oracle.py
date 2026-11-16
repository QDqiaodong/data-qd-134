"""Independent small-graph oracle for the clearing pipeline.

Nothing here imports venue_clearing.graph or venue_clearing.dataset logic for
the answer: edges are recomputed from the raw records by an O(P*C) scan, and
the whole maximum-matching solution set is enumerated by brute force.  That
makes it suitable for checking the structural classifier and the persisted
report on graphs of up to about 7x7.
"""
import sqlite3

def edges_by_scan(payments,credits):
    """payments/credits: dict id -> (venue,currency,amount,items)."""
    edges=set()
    for payment_id,(venue,currency,amount,aliases) in payments.items():
        for credit_id,(cvenue,ccurrency,camount,references) in credits.items():
            if venue==cvenue and currency==ccurrency and amount==camount:
                if set(aliases)&set(references):edges.add((payment_id,credit_id))
    return edges

def all_solutions(left,right,adjacency):
    """Enumerate every maximum matching.  Returns (size, pairs, can_unmatched).

    `pairs` is the set of edges that occur in at least one maximum matching;
    `can_unmatched` the set of vertices left free by at least one.
    """
    left=list(left)
    best=[-1];solutions=[]
    def walk(i,used,chosen):
        if i==len(left):
            if len(chosen)>best[0]:best[0]=len(chosen);solutions.clear();solutions.append(dict(chosen))
            elif len(chosen)==best[0]:solutions.append(dict(chosen))
            return
        walk(i+1,used,chosen)
        u=left[i]
        for v in adjacency.get(u,()):
            if v not in used:
                used.add(v);chosen[u]=v
                walk(i+1,used,chosen)
                del chosen[u];used.remove(v)
    walk(0,set(),{})
    pairs=set();can=set()
    for solution in solutions:
        used=set(solution.values())
        pairs.update(solution.items())
        can.update(set(left)-set(solution))
        can.update(set(right)-used)
    return best[0],pairs,can

def classify(payments,credits,pins):
    """Expected report content from raw records and manual pins.

    pins: dict payment_id -> credit_id.
    """
    edges=edges_by_scan(payments,credits)
    pinned_payments=set(pins);pinned_credits=set(pins.values())
    live_payments=[p for p in payments if p not in pinned_payments]
    live_credits=[c for c in credits if c not in pinned_credits]
    adjacency={p:[] for p in live_payments}
    for p,c in edges:
        if p in pinned_payments or c in pinned_credits:continue
        adjacency[p].append(c)
    size,pairs,can=all_solutions(live_payments,live_credits,adjacency)
    rows={}
    for payment_id in payments:
        if payment_id in pins:
            rows[('P',payment_id)]={'id':payment_id,'status':'pinned',
                                    'possible_peers':[pins[payment_id]],'can_be_unmatched':False}
            continue
        possible=sorted(c for p,c in pairs if p==payment_id)
        free=payment_id in can
        status=('unmatched' if not possible else
                'forced' if len(possible)==1 and not free else 'ambiguous')
        rows[('P',payment_id)]={'id':payment_id,'status':status,
                                'possible_peers':possible,'can_be_unmatched':free}
    for credit_id in credits:
        owner=[p for p,c in pins.items() if c==credit_id]
        if owner:
            rows[('C',credit_id)]={'id':credit_id,'status':'pinned',
                                   'possible_peers':owner,'can_be_unmatched':False}
            continue
        possible=sorted(p for p,c in pairs if c==credit_id)
        free=credit_id in can
        status=('unmatched' if not possible else
                'forced' if len(possible)==1 and not free else 'ambiguous')
        rows[('C',credit_id)]={'id':credit_id,'status':status,
                               'possible_peers':possible,'can_be_unmatched':free}
    return {'generation':None,'maximum_pairs':len(pins)+size,
            'payments':[rows[('P',p)] for p in sorted(payments)],
            'credits':[rows[('C',c)] for c in sorted(credits)]}

def load(db):
    """Read the facts needed by the oracle straight from SQLite."""
    conn=sqlite3.connect(db)
    try:
        payments={row[0]:(row[1],row[2],row[3],
                          [item[0] for item in conn.execute(
                              'SELECT alias FROM payment_alias WHERE payment_id=? ORDER BY alias',(row[0],))])
                  for row in conn.execute('SELECT payment_id,venue,currency,amount_minor FROM payments')}
        credits={row[0]:(row[1],row[2],row[3],
                         [item[0] for item in conn.execute(
                             'SELECT reference FROM credit_reference WHERE credit_id=? ORDER BY reference',(row[0],))])
                 for row in conn.execute('SELECT credit_id,venue,currency,amount_minor FROM credits')}
        pins={row[0]:row[1] for row in conn.execute('SELECT payment_id,credit_id FROM bindings')}
        stored_edges={tuple(row) for row in conn.execute('SELECT payment_id,credit_id FROM edges')}
    finally:conn.close()
    return payments,credits,pins,stored_edges
