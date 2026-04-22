package com.example.stmtgen.dto;

import jakarta.validation.constraints.Digits;
import jakarta.validation.constraints.Size;

import java.math.BigDecimal;

public record StmtRec(
        @Size(max = 8)
        String stmCustId,

        @Size(max = 6)
        String stmPeriod,

        @Digits(integer = 7, fraction = 2)
        BigDecimal stmBal,

        @Digits(integer = 7, fraction = 2)
        BigDecimal stmInterest,

        @Digits(integer = 7, fraction = 2)
        BigDecimal stmTotal
) {
}
