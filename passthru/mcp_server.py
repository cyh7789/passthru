"""MCP server for agents. One server process = one tenant (PASSTHRU_TENANT).

Every tool takes a tenant arg anyway. If it doesn't match the bound tenant the call is
refused and an AccessDenied node is written to the bound tenant's graph, so the attempts
are countable later. Agents can claim and run cases, read the blind review queue and the
active procedure. They can't deny, edit rules or swap procedures; that stays with humans.
"""
import os
from .db import client, tenant_graph, TenantError
from .sched import next_cases
from . import flow, review, procedure


class Tools:
    def __init__(self, tenant, agent="mcp-agent"):
        self.tenant, self.agent = tenant, agent
        self.db = client()
        self.g = tenant_graph(self.db, tenant, bound_tenant=tenant)

    def _check(self, tool, tenant):
        try:
            tenant_graph(self.db, tenant, bound_tenant=self.tenant)
        except TenantError as e:
            self.g.query("""CREATE (:AccessDenied {tool:$tool, bound:$b, asked:$a, agent:$ag, at:timestamp()})""",
                         {"tool": tool, "b": self.tenant, "a": tenant, "ag": self.agent})
            return {"error": "denied", "reason": str(e)}
        return None

    def queue(self, tenant, n=5):
        if err := self._check("queue", tenant):
            return err
        return [{"id": c, "version": v, "deadline": d} for c, v, d in next_cases(self.g, n)]

    def claim_next(self, tenant):
        if err := self._check("claim_next", tenant):
            return err
        for cid, v, _ in next_cases(self.g, 5):
            if flow.claim(self.g, cid, v, self.agent):
                return {"id": cid, "version": v + 1}
        return {"id": None}

    def process(self, tenant, case_id, version):
        """Run the rule path on a case this agent claimed and record it (CAS on the claim)."""
        if err := self._check("process", tenant):
            return err
        outcome, reason, path = flow.decide(self.g, case_id, db=self.db, tenant=self.tenant)
        ok = flow.record(self.g, case_id, version, outcome, reason, path, self.agent)
        return {"id": case_id, "recorded": ok, "outcome": outcome, "reason": reason, "path": path}

    def case_path(self, tenant, case_id):
        if err := self._check("case_path", tenant):
            return err
        rows = self.g.query("""MATCH (c:Case {id:$id}) OPTIONAL MATCH (c)-[r:DECIDED]->(d:Decision)
                               RETURN c.status, d.outcome, d.reason, coalesce(r.path_json, c.path_json)""",
                            {"id": case_id}).result_set
        if not rows:
            return {"error": "not found"}
        st, out, why, path = rows[0]
        return {"id": case_id, "status": st, "outcome": out, "reason": why, "path_json": path}

    def review_queue(self, tenant):
        if err := self._check("review_queue", tenant):
            return err
        return review.list_queue(self.tenant)

    def active_procedure(self, tenant, service_code):
        if err := self._check("active_procedure", tenant):
            return err
        return procedure.active(self.g, service_code) or {"error": "no procedure"}


DESC = {
    "queue": "Peek the next queued cases for your tenant, at-risk first, then earliest deadline.",
    "claim_next": "Claim the next queued case (version CAS). Returns id + version to pass to process.",
    "process": "Run cob, procedure and rule matching on a case you claimed and record the result.",
    "case_path": "Status, decision and the query path behind it for one case.",
    "review_queue": "Blind human review queue (no patient identifiers).",
    "active_procedure": "Active procedure version and steps for a service code.",
}


def build(tenant, agent="mcp-agent"):
    from mcp.server.fastmcp import FastMCP
    t = Tools(tenant, agent)
    mcp = FastMCP(f"passthru-{tenant}")
    for name, desc in DESC.items():
        mcp.add_tool(getattr(t, name), name=name, description=desc)
    return mcp, t


if __name__ == "__main__":
    build(os.environ["PASSTHRU_TENANT"], os.getenv("PASSTHRU_AGENT", "mcp-agent"))[0].run()
