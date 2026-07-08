package com.example.tenant;

/**
 * Central constants for the tenant API.
 * Used by TenantService to construct API calls.
 */
public class ApiConstants {

    public static final String BASE_TENANT_URL = "https://api.enterprise.com/v1";
    public static final String INTERNAL_BASE_URL = "https://internal.enterprise.com/v2";
    public static final String API_KEY_HEADER = "X-Api-Key";
    public static final int DEFAULT_TIMEOUT_MS = 5000;

    private ApiConstants() {}
}
