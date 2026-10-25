"""gateways and model policies are tenant-wide

Moves Gateway and ModelPolicy resources from team namespaces into the tenant's
`shared` namespace. A name already taken there gets the namespace as a prefix
(`<namespace>-<name>`); agents that referred to it need updating.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25 06:55:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

KINDS = ("Gateway", "ModelPolicy")


def upgrade() -> None:
    conn = op.get_bind()
    resources = sa.table(
        "resources", sa.column("id", sa.String), sa.column("tenant_id", sa.String), sa.column("namespace", sa.String),
        sa.column("kind", sa.String), sa.column("name", sa.String), sa.column("document", sa.JSON),
    )
    rows = conn.execute(
        sa.select(resources.c.id, resources.c.tenant_id, resources.c.namespace, resources.c.kind, resources.c.name,
                  resources.c.document).where(resources.c.kind.in_(KINDS), resources.c.namespace != "shared")
    ).all()
    taken = {
        (r.tenant_id, r.kind, r.name)
        for r in conn.execute(
            sa.select(resources.c.tenant_id, resources.c.kind, resources.c.name)
            .where(resources.c.kind.in_(KINDS), resources.c.namespace == "shared")
        ).all()
    }
    for row in rows:
        name = row.name
        if (row.tenant_id, row.kind, name) in taken:
            name = f"{row.namespace}-{row.name}"[:63]
        taken.add((row.tenant_id, row.kind, name))
        document = dict(row.document or {})
        document["metadata"] = {**(document.get("metadata") or {}), "name": name, "namespace": "shared"}
        conn.execute(
            resources.update().where(resources.c.id == row.id).values(namespace="shared", name=name, document=document)
        )


def downgrade() -> None:
    pass  # where each resource came from is not kept; they stay in `shared`
