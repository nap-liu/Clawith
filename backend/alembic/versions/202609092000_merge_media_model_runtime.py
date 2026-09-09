"""Join enterprise media models with the shared runtime migration chain."""

revision = "merge_media_model_runtime"
down_revision = ("model_pool_capabilities", "unfinished_turn_indexes")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
