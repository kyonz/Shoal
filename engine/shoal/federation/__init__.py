"""Federated tenants: a hub manages tenants that run on their own member deployments (architecture §11.2)."""

from shoal.federation.hub import FederationHub
from shoal.federation.member import FederationMember

__all__ = ["FederationHub", "FederationMember"]
