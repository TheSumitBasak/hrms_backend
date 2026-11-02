"""Demo data for development: one Indian organization with every module in use.

Enabled by ``AUTO_SEED=true`` (applied at startup) or ``python -m app.cli seed-demo``.
"""

from app.demo.seeder import DEMO_ORGANIZATION_CODE, DemoSeedError, DemoSeedReport, seed_demo

__all__ = ["DEMO_ORGANIZATION_CODE", "DemoSeedError", "DemoSeedReport", "seed_demo"]
