package com.example.stmtgen.tasklet;

import com.example.stmtgen.service.StmtgenFileService;
import org.springframework.stereotype.Component;

@Component
public class CloseFilesTasklet {

    private final StmtgenFileService stmtgenFileService;

    public CloseFilesTasklet(StmtgenFileService stmtgenFileService) {
        this.stmtgenFileService = stmtgenFileService;
    }

    public void close() {
        stmtgenFileService.closeInput();
        stmtgenFileService.closeOutput();
    }
}
