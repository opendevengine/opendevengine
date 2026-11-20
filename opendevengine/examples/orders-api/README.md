# orders-api

## Order service for checkout

Accepts new orders from the storefront, saves them through the order
repository and writes an order created event to the outbox for downstream
consumers such as confirmation email.
