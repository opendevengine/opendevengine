package example.orders;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

@Repository
public class OrderRepository {
    private final JdbcClient jdbc;

    public OrderRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    public Order insert(String email, String itemsJson) {
        var id = java.util.UUID.randomUUID().toString();
        jdbc.sql("INSERT INTO orders (id, email, items_json) VALUES (?, ?, ?)")
            .params(id, email, itemsJson)
            .update();
        return new Order(id, email);
    }
}
