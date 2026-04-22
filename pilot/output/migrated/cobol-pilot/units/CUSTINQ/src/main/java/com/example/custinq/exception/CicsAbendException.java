package com.example.custinq.exception;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.ResponseStatus;

@ResponseStatus(HttpStatus.INTERNAL_SERVER_ERROR)
public class CicsAbendException extends RuntimeException {

    private final String abcode;

    public CicsAbendException(String abcode, String message) {
        super(message);
        this.abcode = abcode;
    }

    public String getAbcode() {
        return abcode;
    }
}
