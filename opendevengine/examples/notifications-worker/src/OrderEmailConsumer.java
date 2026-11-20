package example.notifications;

import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

@Component
public class OrderEmailConsumer {
    private final EmailClient email;

    public OrderEmailConsumer(EmailClient email) {
        this.email = email;
    }

    @KafkaListener(topics = "order.created")
    public void sendConfirmation(OrderCreated event) {
        email.sendOrderConfirmation(event.email(), event.orderId());
    }
}
