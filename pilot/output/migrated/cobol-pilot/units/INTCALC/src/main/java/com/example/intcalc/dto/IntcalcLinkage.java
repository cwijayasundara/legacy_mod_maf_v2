package com.example.intcalc.dto;

import jakarta.validation.constraints.Digits;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

import java.math.BigDecimal;

public record IntcalcLinkage(
        @NotNull
        @Digits(integer = 18, fraction = 2)
        BigDecimal lnkBalance,

        @NotNull
        @Digits(integer = 1, fraction = 9)
        BigDecimal lnkRate,

        @Digits(integer = 18, fraction = 9)
        BigDecimal lnkInterest,

        @Size(max = 1)
        String lnkOk,

        @Size(max = 20)
        String lnkMsg
) {
}
