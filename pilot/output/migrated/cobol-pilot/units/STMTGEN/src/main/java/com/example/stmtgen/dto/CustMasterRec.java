package com.example.stmtgen.dto;

import jakarta.validation.constraints.Size;

public record CustMasterRec(
        @Size(max = 8)
        String cmrCustId,

        @Size(max = 100)
        String cmrData
) {
}
