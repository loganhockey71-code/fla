"""Free, legal market-data adapters feeding the local data lake (see datalake.py). Research/data-collection only -
nothing here is wired into live trading. New sources are added as a new module exposing ADAPTERS (a list of
Adapter instances); nothing else in the project needs to change (see base.py, and DATA_SOURCES.md for the catalog
this was built from)."""
