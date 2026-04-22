package com.example.stmtgen.dto;

import jakarta.validation.Valid;
import jakarta.validation.constraints.Digits;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

import java.math.BigDecimal;

public record CustomerRec(
        @Size(max = 8)
        String custId,

        @Size(max = 30)
        String custLast,

        @Size(max = 20)
        String custFirst,

        @Digits(integer = 7, fraction = 2)
        BigDecimal custBal,

        @Digits(integer = 1, fraction = 3)
        BigDecimal custRate,

        @Size(max = 1)
        String custStatus,

        @Size(max = 8)
        String custOpenDate,

        @Size(max = 24)
        String custExtra,

        @Valid
        @NotNull
        CustExtraView custExtraView
) {
}
