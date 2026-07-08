package com.example.api;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/**
 * Sample Java class demonstrating various java.net.http.HttpClient usage patterns.
 * Used as a test fixture for the ai-agents REST API analyzer.
 */
public class ApiService {

    private final HttpClient client = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .build();

    private final String baseUrl = "https://api.example.com";

    // ── Pattern 1: Simple GET with literal URL ─────────────────────────────

    public String listUsers() throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create("https://api.example.com/users"))
                .header("Accept", "application/json")
                .GET()
                .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return response.body();
    }

    // ── Pattern 2: GET with path param (dynamic URL) ───────────────────────

    public String getUserById(String userId) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/users/" + userId))
                .header("Accept", "application/json")
                .header("Authorization", "Bearer mytoken123")
                .GET()
                .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return response.body();
    }

    // ── Pattern 3: GET with query params in literal URL ────────────────────

    public String searchUsers(String query, int page) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create("https://api.example.com/users/search?q=john&page=1&size=20"))
                .header("Accept", "application/json")
                .GET()
                .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return response.body();
    }

    // ── Pattern 4: POST with JSON body ────────────────────────────────────

    public String createUser(String jsonBody) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create("https://api.example.com/users"))
                .header("Content-Type", "application/json")
                .header("Accept", "application/json")
                .header("Authorization", "Bearer mytoken123")
                .POST(HttpRequest.BodyPublishers.ofString("{\"name\":\"John\",\"email\":\"john@example.com\"}"))
                .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return response.body();
    }

    // ── Pattern 5: PUT with path param and JSON body ───────────────────────

    public String updateUser(String userId, String jsonBody) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/users/" + userId))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer mytoken123")
                .PUT(HttpRequest.BodyPublishers.ofString(jsonBody))
                .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return response.body();
    }

    // ── Pattern 6: DELETE with path param ─────────────────────────────────

    public void deleteUser(String userId) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/users/" + userId))
                .header("Authorization", "Bearer mytoken123")
                .DELETE()
                .build();

        client.send(request, HttpResponse.BodyHandlers.discarding());
    }

    // ── Pattern 7: PATCH via .method() ────────────────────────────────────

    public String patchUser(String userId, String patchBody) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/users/" + userId))
                .header("Content-Type", "application/json-patch+json")
                .header("Authorization", "Bearer mytoken123")
                .method("PATCH", HttpRequest.BodyPublishers.ofString(patchBody))
                .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return response.body();
    }

    // ── Pattern 8: GET with Cookie header ─────────────────────────────────

    public String getSecureResource(String resourceId) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create("https://api.example.com/secure/resources/" + resourceId))
                .header("Accept", "application/json")
                .header("Cookie", "session=abc123; user_id=42; csrf_token=xyz789")
                .GET()
                .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return response.body();
    }

    // ── Pattern 9: Multi-header shorthand ─────────────────────────────────

    public String getWithMultipleHeaders() throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create("https://api.example.com/reports"))
                .headers(
                    "Accept", "application/json",
                    "X-Request-ID", "req-001",
                    "X-Correlation-ID", "corr-abc"
                )
                .GET()
                .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return response.body();
    }

    // ── Pattern 10: URI passed directly to newBuilder() ──────────────────

    public String getProduct(String productId) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(
                URI.create("https://api.example.com/products/" + productId))
                .header("Accept", "application/json")
                .GET()
                .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return response.body();
    }
}
