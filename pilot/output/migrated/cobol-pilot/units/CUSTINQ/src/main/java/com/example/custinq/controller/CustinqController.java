package com.example.custinq.controller;

import com.example.custinq.dto.CustinqRequest;
import com.example.custinq.dto.CustinqResponse;
import com.example.custinq.service.CustinqService;
import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/custinq")
public class CustinqController {

    private final CustinqService custinqService;

    public CustinqController(CustinqService custinqService) {
        this.custinqService = custinqService;
    }

    @PostMapping
    public ResponseEntity<CustinqResponse> inquireCustomer(
            @Valid @RequestBody CustinqRequest request,
            @RequestParam(value = "aid", required = false) String aid) {
        return ResponseEntity.ok(custinqService.inquireCustomer(request, aid));
    }
}
