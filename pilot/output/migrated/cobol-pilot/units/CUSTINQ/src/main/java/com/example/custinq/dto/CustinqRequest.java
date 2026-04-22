package com.example.custinq.dto;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

public record CustinqRequest(
        @NotNull
        @Size(max = 8)
        String commCustId
) {
}
