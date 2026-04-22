package com.example.intcalc;

import com.example.intcalc.service.IntcalcService;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;

import static org.junit.jupiter.api.Assertions.assertNotNull;

@SpringBootTest
class ContextLoadTest {

    private final IntcalcService intcalcService;

    ContextLoadTest(IntcalcService intcalcService) {
        this.intcalcService = intcalcService;
    }

    @Test
    void contextLoads() {
        assertNotNull(intcalcService);
    }
}
