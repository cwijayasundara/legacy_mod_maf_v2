package com.example.intcalc.dto;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

public record LnkStatus(
        @NotNull
        @Size(min = 1, max = 1)
        String lnkOk,
        @NotNull
        @Size(max = 30)
        String lnkMsg
) {
}
