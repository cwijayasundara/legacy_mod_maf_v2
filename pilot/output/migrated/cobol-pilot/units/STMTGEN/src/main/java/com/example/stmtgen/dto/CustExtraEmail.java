package com.example.stmtgen.dto;

import jakarta.validation.constraints.Size;

public record CustExtraEmail(
        @Size(max = 24)
        String custEmail
) implements CustExtraView {
}
