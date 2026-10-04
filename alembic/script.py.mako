<%
    # Parent revision(s), rendered without trailing whitespace for the first
    # revision (keeps generated files clean for `ruff check .`).
    _revises = down_revision
    if isinstance(_revises, (list, tuple)):
        _revises = ", ".join(_revises)
    _revises = _revises or "(none)"
%>"""${message}

Revision ID: ${up_revision}
Revises: ${_revises}
Create Date: ${create_date}

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Upgrade schema."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Downgrade schema."""
    ${downgrades if downgrades else "pass"}
