"""One thin HTTP client per external service. Clients speak the wire format and raise typed
errors; sync logic lives in ``syncshelf.sync`` (SPEC §2.5)."""
