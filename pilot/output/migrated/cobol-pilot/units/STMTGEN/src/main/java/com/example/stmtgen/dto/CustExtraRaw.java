package com.example.stmtgen.dto;

import jakarta.validation.constraints.Size;

public record CustExtraRaw(
        @Size(max = 24)
        String custExtra
) implements CustExtraView {
}
