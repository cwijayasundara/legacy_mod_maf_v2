package com.example.intcalc.dto;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotNull;

import java.math.BigDecimal;

public record IntcalcResult(
        @NotNull
        BigDecimal lnkInterest,
        @NotNull
        @Valid
        LnkStatus lnkStatus
) {
}
