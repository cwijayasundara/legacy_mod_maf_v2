package com.example;

import com.amazonaws.services.lambda.runtime.Context;
import com.amazonaws.services.lambda.runtime.RequestHandler;
import com.amazonaws.services.lambda.runtime.events.SQSEvent;

import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;

import java.nio.charset.StandardCharsets;

/**
 * Generate a minimal PDF-placeholder invoice object in S3 for every order
 * accepted upstream. One object per order.
 */
public class InvoiceHandler implements RequestHandler<SQSEvent, String> {

    private final S3Client s3 = S3Client.builder().build();

    @Override
    public String handleRequest(SQSEvent event, Context ctx) {
        String bucket = System.getenv("INVOICES_BUCKET");
        int generated = 0;
        for (SQSEvent.SQSMessage msg : event.getRecords()) {
            String key = "invoice-" + extractOrderId(msg.getBody()) + ".txt";
            byte[] body = ("INVOICE " + key).getBytes(StandardCharsets.UTF_8);
            s3.putObject(
                PutObjectRequest.builder().bucket(bucket).key(key).build(),
                RequestBody.fromBytes(body)
            );
            generated++;
        }
        return "generated=" + generated;
    }

    private String extractOrderId(String json) {
        int idx = json.indexOf("\"orderId\"");
        if (idx < 0) return "unknown";
        int start = json.indexOf('"', idx + 10) + 1;
        int end = json.indexOf('"', start);
        return (start > 0 && end > start) ? json.substring(start, end) : "unknown";
    }
}
