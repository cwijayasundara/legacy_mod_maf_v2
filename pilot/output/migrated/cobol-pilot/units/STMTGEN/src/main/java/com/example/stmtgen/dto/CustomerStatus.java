package com.example.stmtgen.dto;

public enum CustomerStatus {
    ACTIVE("A"),
    SUSPENDED("S"),
    CLOSED("C");

    private final String code;

    CustomerStatus(String code) {
        this.code = code;
    }

    public String code() {
        return code;
    }

    public static CustomerStatus fromCode(String code) {
        for (CustomerStatus value : values()) {
            if (value.code.equals(code)) {
                return value;
            }
        }
        throw new IllegalArgumentException("Unknown customer status code: " + code);
    }
}
