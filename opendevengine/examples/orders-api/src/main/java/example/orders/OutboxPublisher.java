package example.orders;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Scheduled;

public class OutboxPublisher {
    private final OutboxRepository outbox;
    private final KafkaTemplate<String, OrderCreated> kafka;

    public OutboxPublisher(OutboxRepository outbox, KafkaTemplate<String, OrderCreated> kafka) {
        this.outbox = outbox;
        this.kafka = kafka;
    }

    @Scheduled(fixedDelay = 5000)
    public void publishPending() throws Exception {
        for (var event : outbox.pending()) {
            kafka.send(event.topic(), event.orderId(), event.payload()).get();
            outbox.markPublished(event.id());
        }
    }
}
