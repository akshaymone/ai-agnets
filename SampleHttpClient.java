import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/**
 * SampleHttpClient.java
 * ---------------------
 * Dummy Java HTTP client used to exercise the ai-agents Tree-sitter extractor.
 * Contains three outgoing HTTP calls that the tool should detect and convert
 * to OpenAPI 3.1.0 specifications:
 *
 *   1. GET  /api/v1/users/{userId}          — fetchUser
 *   2. POST /api/v1/orders                  — createOrder
 *   3. DELETE /api/v1/sessions/{sessionId}  — invalidateSession
 */
public class SampleHttpClient {

    private static final String BASE_URL = "https://api.example.com";
    private final HttpClient httpClient;

    public SampleHttpClient() {
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();
    }

    // -----------------------------------------------------------------------
    // 1.  GET /api/v1/users/{userId}
    // -----------------------------------------------------------------------
    public String fetchUser(String userId, String authToken) throws Exception {
        String sTargetURL = BASE_URL + "/api/v1/users/" + userId;

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(sTargetURL))
                .timeout(Duration.ofSeconds(5))
                .header("Authorization", "Bearer " + authToken)
                .header("Accept", "application/json")
                .GET()
                .build();

        HttpResponse<String> response = httpClient.send(
                request,
                HttpResponse.BodyHandlers.ofString()
        );

        if (response.statusCode() != 200) {
            throw new RuntimeException("Failed to fetch user. Status: " + response.statusCode());
        }

        return response.body();
    }

    // -----------------------------------------------------------------------
    // 2.  POST /api/v1/orders
    // -----------------------------------------------------------------------
    public String createOrder(String authToken, String orderPayloadJson) throws Exception {
        String sTargetURL = BASE_URL + "/api/v1/orders";

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(sTargetURL))
                .timeout(Duration.ofSeconds(15))
                .header("Authorization", "Bearer " + authToken)
                .header("Content-Type", "application/json")
                .header("Accept", "application/json")
                .header("X-Request-ID", java.util.UUID.randomUUID().toString())
                .POST(HttpRequest.BodyPublishers.ofString(orderPayloadJson))
                .build();

        HttpResponse<String> response = httpClient.send(
                request,
                HttpResponse.BodyHandlers.ofString()
        );

        if (response.statusCode() != 201) {
            throw new RuntimeException("Failed to create order. Status: " + response.statusCode());
        }

        return response.body();
    }

    // -----------------------------------------------------------------------
    // 3.  DELETE /api/v1/sessions/{sessionId}
    // -----------------------------------------------------------------------
    public void invalidateSession(String sessionId, String authToken) throws Exception {
        String sessionEndpointURL = BASE_URL + "/api/v1/sessions/" + sessionId;

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(sessionEndpointURL))
                .timeout(Duration.ofSeconds(5))
                .header("Authorization", "Bearer " + authToken)
                .header("Accept", "application/json")
                .method("DELETE", HttpRequest.BodyPublishers.noBody())
                .build();

        HttpResponse<Void> response = httpClient.send(
                request,
                HttpResponse.BodyHandlers.discarding()
        );

        if (response.statusCode() != 204) {
            throw new RuntimeException("Failed to invalidate session. Status: " + response.statusCode());
        }
    }

    // -----------------------------------------------------------------------
    // Main — only used for local sanity-testing of the Java file itself
    // -----------------------------------------------------------------------
    public static void main(String[] args) {
        System.out.println("SampleHttpClient loaded. Run ai-agents to extract OpenAPI spec.");
    }
}
