import os
from falkordb import FalkorDB

TENANTS = ("tenant_us_acme", "tenant_in_bharat")
PROVIDER = "provider_demo"


class TenantError(Exception):
    pass


def client():
    return FalkorDB(host=os.getenv("FALKOR_HOST", "localhost"), port=int(os.getenv("FALKOR_PORT", 6379)))


def tenant_graph(db, tenant, bound_tenant=None):
    # agents are bound to one tenant; asking for another one is refused here
    if tenant not in TENANTS:
        raise TenantError(f"unknown tenant {tenant}")
    if bound_tenant and tenant != bound_tenant:
        raise TenantError(f"cross-tenant access {bound_tenant} -> {tenant}")
    return db.select_graph(tenant)
