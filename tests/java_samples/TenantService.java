package com.example.tenant;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

/**
 * Real-world scenario: BASE_TENANT_URL is a static final in a DIFFERENT class (ApiConstants).
 * This tests the whole-project symbol resolution fallback in SymbolIndex.
 *
 * Also tests:
 *  - Static constant from same class (TENANT_PATH_PREFIX)
 *  - @Value-style property reference (commented, for future)
 *  - Header present alongside dynamic URL
 *  - Cookies
 */
public class TenantService {

    private final HttpClient client = HttpClient.newBuilder().build();

    // Same-class constant (should be found in same-file pass)
    private static final String TENANT_PATH_PREFIX = "/tenants";

    /**
     * SCENARIO 1: Static constant in a DIFFERENT class (ApiConstants.BASE_TENANT_URL).
     * The SymbolIndex must find it via whole-project fallback.
     * Expected full URL: https://api.enterprise.com/v1/system/health
     */
    public void checkSystemHealth() throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(ApiConstants.BASE_TENANT_URL + "/system/health"))
                .header("Accept", "application/json")
                .GET()
                .build();

        client.send(request, HttpResponse.BodyHandlers.ofString());
    }

    /**
     * SCENARIO 2: Same-class constant + method parameter (dynamic path segment).
     * Expected template: https://api.enterprise.com/v1/tenants/{tenantId}
     */
    public void getTenant(String tenantId) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(ApiConstants.BASE_TENANT_URL + TENANT_PATH_PREFIX + "/" + tenantId))
                .header("Accept", "application/json")
                .header("Authorization", "Bearer mytoken")
                .GET()
                .build();

        client.send(request, HttpResponse.BodyHandlers.ofString());
    }

    /**
     * SCENARIO 3: POST with JSON body and cross-class constant URL.
     * Expected: POST https://api.enterprise.com/v1/tenants
     */
    public void createTenant(String jsonBody) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(ApiConstants.BASE_TENANT_URL + TENANT_PATH_PREFIX))
                .header("Content-Type", "application/json")
                .header("Accept", "application/json")
                .header(ApiConstants.API_KEY_HEADER, "my-api-key-value")
                .POST(HttpRequest.BodyPublishers.ofString(jsonBody))
                .build();

        client.send(request, HttpResponse.BodyHandlers.ofString());
    }

    /**
     * SCENARIO 4: Internal URL from a different constant field.
     * Expected: GET https://internal.enterprise.com/v2/metrics
     */
    public void getInternalMetrics() throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(ApiConstants.INTERNAL_BASE_URL + "/metrics"))
                .header("Accept", "application/json")
                .header("Cookie", "session=xyz; role=admin")
                .GET()
                .build();

        client.send(request, HttpResponse.BodyHandlers.ofString());
    }
}
