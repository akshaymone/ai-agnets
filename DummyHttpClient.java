package com.example;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

public class DummyHttpClient {
    
    public void getUserData() throws Exception {
        String sTargetURL = "https://api.example.com/v1/users/12345";
        String token = "my-bearer-token";
        
        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(sTargetURL))
            .header("Authorization", "Bearer " + token)
            .header("Accept", "application/json")
            .GET()
            .build();
            
        HttpClient client = HttpClient.newHttpClient();
        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        System.out.println(response.body());
    }

    public void updatePost() throws Exception {
        String urlStr = "https://api.example.com/v1/posts";
        String contentType = "application/json";
        String bodyJson = "{\"title\":\"Hello World\"}";
        
        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(urlStr))
            .header("Content-Type", contentType)
            .POST(HttpRequest.BodyPublishers.ofString(bodyJson))
            .build();
            
        HttpClient client = HttpClient.newHttpClient();
        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        System.out.println(response.body());
    }
}
