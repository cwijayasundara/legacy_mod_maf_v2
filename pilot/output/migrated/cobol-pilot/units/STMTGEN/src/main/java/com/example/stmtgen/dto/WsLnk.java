package com.example.stmtgen.dto;

import jakarta.validation.constraints.Size;

public record WsLnk(
        @Size(max = 1)
        String lnkOk,

        @Size(max = 30)
        String lnkMsg
) {
}
