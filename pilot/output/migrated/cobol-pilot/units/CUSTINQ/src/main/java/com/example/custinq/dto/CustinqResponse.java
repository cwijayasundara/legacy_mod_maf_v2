package com.example.custinq.dto;

import jakarta.validation.constraints.Size;

public record CustinqResponse(
        @Size(max = 8)
        String commCustId,
        @Size(max = 1)
        String commResult,
        String commCustOut,
        CustomerRec customerRec
) {
}
