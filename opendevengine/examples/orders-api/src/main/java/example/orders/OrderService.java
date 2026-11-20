package example.orders;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class OrderService {
    private final OrderRepository orders;
    private final OutboxRepository outbox;

    public OrderService(OrderRepository orders, OutboxRepository outbox) {
        this.orders = orders;
        this.outbox = outbox;
    }

    @Transactional
    public String placeOrder(CheckoutRequest request) {
        var order = orders.insert(request.email(), request.items());
        outbox.append("order.created", order.id(), order.email());
        return order.id();
    }
}
