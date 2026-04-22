package com.example.stmtgen;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.TestPropertySource;

@SpringBootTest
@ActiveProfiles("test")
@TestPropertySource(properties = {
        "spring.datasource.url=jdbc:h2:mem:test;DB_CLOSE_DELAY=-1;DB_CLOSE_ON_EXIT=FALSE",
        "spring.datasource.driver-class-name=org.h2.Driver",
        "spring.datasource.username=sa",
        "spring.datasource.password=",
        "stmtgen.input.path=target/test-data/PROD.CUST.MASTER",
        "stmtgen.output.path=target/test-data/PROD.STMT.DAILY",
        "stmtgen.period=202401"
})
class ContextLoadTest {

    @Test
    void contextLoads() {
    }
}
