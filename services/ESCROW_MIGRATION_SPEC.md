# WIP: Persistent Webhook Idempotency & Atomic Stock Reservation Spec

Target branch for refactoring `InMemoryIdempotencyStore` and `InventoryReservationGuard` into ACID SQLite tables (`webhook_events`, `payment_dead_letters`, and `stock_items.reserved_until`).
