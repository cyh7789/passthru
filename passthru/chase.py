"""Shortest-path chase from a Case to each missing DocRequirement via FalkorDB algo.SPpaths.

Schema (APPLIES_TO points at Service, so we traverse both directions):
  Case -[:FOR_SERVICE]-> Service <-[:APPLIES_TO]- Rule
       -[:HAS_CRITERION]-> Criterion -[:NEEDS_DOC]-> DocRequirement
"""
SP = """MATCH (c:Case {id:$id}), (d:DocRequirement {type:$dt})
        CALL algo.SPpaths({sourceNode:c, targetNode:d,
                           relTypes:['FOR_SERVICE','APPLIES_TO','HAS_CRITERION','NEEDS_DOC'],
                           relDirection:'both', pathCount:1, maxLen:6})
        YIELD path, pathWeight
        RETURN toInteger(pathWeight),
               [n IN nodes(path) | labels(n)[0]],
               [n IN nodes(path) | coalesce(n.id, n.type, n.code)],
               [r IN relationships(path) | type(r)]"""


def chase_one(g, case_id, doc_type):
    """One Case -> DocRequirement shortest path, or None if the algo finds nothing."""
    rows = g.query(SP, {"id": case_id, "dt": doc_type}).result_set
    if not rows:
        return None
    length, labels, nodes, rels = rows[0]
    return {
        "step": "chase",
        "doc": doc_type,
        "algo": "algo.SPpaths",
        "length": length,
        "hops": labels,
        "nodes": nodes,
        "rels": rels,
    }


def chase_missing(g, case_id, missing):
    """Chase each missing doc type. Skips docs the algo can't reach; never raises."""
    steps = []
    for dt in missing:
        try:
            step = chase_one(g, case_id, dt)
        except Exception:
            continue
        if step:
            steps.append(step)
    return steps
