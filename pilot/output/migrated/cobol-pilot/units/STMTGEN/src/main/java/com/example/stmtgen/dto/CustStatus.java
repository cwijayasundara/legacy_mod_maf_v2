package com.example.stmtgen.dto;

public enum CustStatus {
    ACTIVE("A"),
    SUSPENDED("S"),
    CLOSED("C");

    private final String code;

    CustStatus(String code) {
        this.code = code;
    }

    public String code() {
        return code;
    }

    public static CustStatus fromCode(String code) {
        for (CustStatus value : values()) {
            if (value.code.equals(code)) {
                return value;
            }
        }
        throw new IllegalArgumentException("Unknown customer status code: " + code);
    }
}
