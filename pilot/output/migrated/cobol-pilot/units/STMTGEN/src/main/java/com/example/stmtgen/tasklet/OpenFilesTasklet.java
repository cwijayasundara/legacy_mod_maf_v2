package com.example.stmtgen.tasklet;

import com.example.stmtgen.service.StmtgenFileService;
import org.springframework.stereotype.Component;

@Component
public class OpenFilesTasklet {

    private final StmtgenFileService stmtgenFileService;

    public OpenFilesTasklet(StmtgenFileService stmtgenFileService) {
        this.stmtgenFileService = stmtgenFileService;
    }

    public void open() {
        stmtgenFileService.openInput();
        stmtgenFileService.openOutput();
    }
}
