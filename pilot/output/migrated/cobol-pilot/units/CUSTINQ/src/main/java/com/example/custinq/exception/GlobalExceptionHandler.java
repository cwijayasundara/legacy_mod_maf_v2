package com.example.custinq.exception;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.time.Instant;
import java.util.Map;

@RestControllerAdvice
public class GlobalExceptionHandler {

    @ExceptionHandler(ArithmeticException.class)
    public ResponseEntity<Map<String, Object>> handleArithmeticException(ArithmeticException ex) {
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(Map.of(
                        "timestamp", Instant.now().toString(),
                        "error", "ARITHMETIC_OVERFLOW",
                        "message", ex.getMessage()
                ));
    }

    @ExceptionHandler(CicsAbendException.class)
    public ResponseEntity<Map<String, Object>> handleCicsAbendException(CicsAbendException ex) {
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(Map.of(
                        "timestamp", Instant.now().toString(),
                        "error", "CICS_ABEND",
                        "abcode", ex.getAbcode(),
                        "message", ex.getMessage()
                ));
    }
}
