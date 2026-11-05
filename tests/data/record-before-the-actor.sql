PRAGMA user_version = 1;
BEGIN TRANSACTION;
CREATE TABLE "beats" (
  "run" INTEGER NOT NULL,
  "at" INTEGER NOT NULL,
  PRIMARY KEY ("run")
) STRICT;
INSERT INTO "beats" VALUES(2,1700000013000);
CREATE TABLE "capacity" (
  "node" TEXT NOT NULL,
  "resource" TEXT NOT NULL,
  "amount" INTEGER NOT NULL,
  "unit" TEXT NOT NULL,
  "declared_by" TEXT NOT NULL,
  "at" INTEGER NOT NULL,
  PRIMARY KEY ("node", "resource")
) STRICT;
INSERT INTO "capacity" VALUES('n1','slots',3,'slots','alpha',1700000005000);
CREATE TABLE "change_text" (
  "change" INTEGER NOT NULL,
  "body" TEXT NOT NULL,
  PRIMARY KEY ("change")
) STRICT;
CREATE TABLE "changes" (
  "id" INTEGER NOT NULL,
  "repo" TEXT NOT NULL,
  "n" INTEGER NOT NULL,
  "number" INTEGER,
  "relation" TEXT NOT NULL,
  "title" TEXT NOT NULL,
  "branch" TEXT NOT NULL,
  "head" TEXT NOT NULL,
  "base" TEXT,
  "position" INTEGER,
  "draft" INTEGER NOT NULL CHECK ("draft" IN (0, 1)),
  "url" TEXT,
  "opened_at" INTEGER NOT NULL,
  "opened_by" TEXT,
  "pushed_at" INTEGER,
  "merged_at" INTEGER,
  "dropped_at" INTEGER,
  "dropped_by" TEXT,
  PRIMARY KEY ("id")
) STRICT;
CREATE TABLE "checks" (
  "change" INTEGER NOT NULL,
  "head" TEXT NOT NULL,
  "name" TEXT NOT NULL,
  "state" TEXT NOT NULL,
  "detail" TEXT,
  "at" INTEGER NOT NULL,
  "by" TEXT NOT NULL,
  PRIMARY KEY ("change", "head", "name")
) STRICT;
CREATE TABLE "comments" (
  "id" INTEGER NOT NULL,
  "number" INTEGER,
  "change" INTEGER,
  "who" TEXT NOT NULL,
  "body" TEXT NOT NULL,
  "at" INTEGER NOT NULL,
  "mark" TEXT,
  "ref" TEXT,
  "source_ref" TEXT,
  PRIMARY KEY ("id")
) STRICT;
INSERT INTO "comments" VALUES(1,1,NULL,'alpha','a comment',1700000003000,NULL,NULL,NULL);
CREATE TABLE "cursors" (
  "consumer" TEXT NOT NULL,
  "seq" INTEGER NOT NULL,
  "at" INTEGER NOT NULL,
  PRIMARY KEY ("consumer")
) STRICT;
INSERT INTO "cursors" VALUES('follower',2,1700000014000);
CREATE TABLE "deps" (
  "id" INTEGER NOT NULL,
  "number" INTEGER NOT NULL,
  "blocker" INTEGER,
  "change" INTEGER,
  "what" TEXT,
  "added_at" INTEGER NOT NULL,
  "added_by" TEXT NOT NULL,
  "lifted_at" INTEGER,
  "lifted_by" TEXT,
  "why" TEXT NOT NULL,
  PRIMARY KEY ("id")
) STRICT;
CREATE TABLE "effort_links" (
  "effort" TEXT NOT NULL,
  "kind" TEXT NOT NULL,
  "target" TEXT NOT NULL,
  "at" INTEGER NOT NULL,
  "by" TEXT NOT NULL,
  "unlinked_at" INTEGER,
  PRIMARY KEY ("effort", "kind", "target")
) STRICT;
INSERT INTO "effort_links" VALUES('0001','item','1',1700000002000,'alpha',NULL);
CREATE TABLE "efforts" (
  "id" TEXT NOT NULL,
  "slug" TEXT NOT NULL,
  "title" TEXT NOT NULL,
  "standing" TEXT NOT NULL,
  "owner" TEXT NOT NULL,
  "created_at" INTEGER NOT NULL,
  "statement" TEXT NOT NULL,
  "inputs" TEXT NOT NULL CHECK (json_valid("inputs")),
  "tools" TEXT NOT NULL CHECK (json_valid("tools")),
  "budget" TEXT NOT NULL CHECK (json_valid("budget")),
  "evidence" TEXT NOT NULL CHECK (json_valid("evidence")),
  "gates" TEXT NOT NULL CHECK (json_valid("gates")),
  "order" TEXT NOT NULL CHECK (json_valid("order")),
  "outcome" TEXT,
  "concluded_at" INTEGER,
  PRIMARY KEY ("id")
) STRICT;
INSERT INTO "efforts" VALUES('0001','e0001','Effort 0001','admitted','owner-a',1700000001000,'Do the thing','{}','{}','{}','{}','{}','{"rule":"unblocks-then-age"}',NULL,NULL);
CREATE TABLE "holds" (
  "id" INTEGER NOT NULL,
  "number" INTEGER,
  "who" TEXT,
  "cause" TEXT NOT NULL,
  "why" TEXT NOT NULL,
  "set_at" INTEGER NOT NULL,
  "set_by" TEXT NOT NULL,
  "lifted_at" INTEGER,
  "lifted_by" TEXT,
  PRIMARY KEY ("id")
) STRICT;
CREATE TABLE "imports" (
  "id" INTEGER NOT NULL,
  "source" TEXT NOT NULL CHECK (json_valid("source")),
  "format" TEXT NOT NULL,
  "started_at" INTEGER NOT NULL,
  "finished_at" INTEGER,
  "counts" TEXT NOT NULL CHECK (json_valid("counts")),
  PRIMARY KEY ("id")
) STRICT;
CREATE TABLE "item_text" (
  "number" INTEGER NOT NULL,
  "body" TEXT NOT NULL,
  PRIMARY KEY ("number")
) STRICT;
INSERT INTO "item_text" VALUES(1,'its body');
CREATE TABLE "items" (
  "number" INTEGER NOT NULL,
  "kind" TEXT NOT NULL,
  "title" TEXT NOT NULL,
  "state" TEXT NOT NULL,
  "role" TEXT,
  "tier" INTEGER,
  "risk" INTEGER,
  "parent" INTEGER,
  "labels" TEXT NOT NULL CHECK (json_valid("labels")),
  "flags" TEXT NOT NULL CHECK (json_valid("flags")),
  "admitted" INTEGER NOT NULL CHECK ("admitted" IN (0, 1)),
  "filed_by" TEXT NOT NULL,
  "must_fix" INTEGER NOT NULL CHECK ("must_fix" IN (0, 1)),
  "templet" INTEGER,
  "review_round" INTEGER NOT NULL,
  "overruled_at" INTEGER,
  "created_at" INTEGER NOT NULL,
  "updated_at" INTEGER NOT NULL,
  "closed_at" INTEGER,
  "source" TEXT NOT NULL,
  "source_ref" TEXT,
  PRIMARY KEY ("number")
) STRICT;
INSERT INTO "items" VALUES(1,'task','a task','ready','alpha',NULL,NULL,NULL,'[]','[]',1,'alpha',0,NULL,0,NULL,1700000002000,1700000002000,NULL,'local',NULL);
CREATE TABLE journal (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at INTEGER NOT NULL,
  actor TEXT NOT NULL,
  via TEXT NOT NULL,
  kind TEXT NOT NULL,
  subject TEXT,
  data TEXT NOT NULL CHECK (json_valid(data)),
  puts TEXT NOT NULL CHECK (json_valid(puts))
) STRICT;
INSERT INTO "journal" VALUES(1,1700000001000,'alpha','cli','effort.file','effort:0001','{"id":"0001","standing":"admitted"}','[["meta",["next:efforts"],{"key":"next:efforts","value":2}],["efforts",["0001"],{"budget":{},"concluded_at":null,"created_at":1700000001000,"evidence":{},"gates":{},"id":"0001","inputs":{},"order":{"rule":"unblocks-then-age"},"outcome":null,"owner":"owner-a","slug":"e0001","standing":"admitted","statement":"Do the thing","title":"Effort 0001","tools":{}}]]');
INSERT INTO "journal" VALUES(2,1700000002000,'alpha','cli','item.file','item:1','{"efforts":["0001"],"kind":"task","number":1,"state":"ready"}','[["meta",["next:items"],{"key":"next:items","value":2}],["items",[1],{"admitted":true,"closed_at":null,"created_at":1700000002000,"filed_by":"alpha","flags":[],"kind":"task","labels":[],"must_fix":false,"number":1,"overruled_at":null,"parent":null,"review_round":0,"risk":null,"role":"alpha","source":"local","source_ref":null,"state":"ready","templet":null,"tier":null,"title":"a task","updated_at":1700000002000}],["item_text",[1],{"body":"its body","number":1}],["effort_links",["0001","item","1"],{"at":1700000002000,"by":"alpha","effort":"0001","kind":"item","target":"1","unlinked_at":null}]]');
INSERT INTO "journal" VALUES(3,1700000003000,'alpha','cli','comment','item:1','{"change":null,"id":1,"mark":null,"number":1}','[["meta",["next:comments"],{"key":"next:comments","value":2}],["comments",[1],{"at":1700000003000,"body":"a comment","change":null,"id":1,"mark":null,"number":1,"ref":null,"source_ref":null,"who":"alpha"}]]');
INSERT INTO "journal" VALUES(4,1700000004000,'alpha','cli','node.declare','node:n1','{"drivers":["process"],"labels":["zone:a"],"name":"n1"}','[["nodes",["n1"],{"declared_at":1700000004000,"drivers":["process"],"labels":["zone:a"],"name":"n1","retired_at":null}]]');
INSERT INTO "journal" VALUES(5,1700000005000,'alpha','cli','capacity.set','node:n1','{"amount":3,"node":"n1","resource":"slots"}','[["capacity",["n1","slots"],{"amount":3,"at":1700000005000,"declared_by":"alpha","node":"n1","resource":"slots","unit":"slots"}]]');
INSERT INTO "journal" VALUES(6,1700000006000,'alpha','cli','task.file','task:1','{"effort":"0001","id":1,"kind":"job","number":null}','[["meta",["next:tasks"],{"key":"next:tasks","value":2}],["tasks",[1],{"attempts":0,"created_at":1700000006000,"effort":"0001","ended_at":null,"flags":[],"id":1,"kind":"job","labels":[],"loop":{"interval_s":0,"max_attempts":1},"needs":{"slots":1},"not_before":null,"number":null,"sandbox":"process","schedule":null,"state":"open","supervision":{"beat_s":null,"expect_s":null}}]]');
INSERT INTO "journal" VALUES(7,1700000007000,'alpha','cli','run.start','run:1','{"attempt":1,"fence":null,"holder":"alpha","node":"n1","reservations":[1],"run":1,"task":1}','[["meta",["next:runs"],{"key":"next:runs","value":2}],["tasks",[1],{"attempts":1,"created_at":1700000006000,"effort":"0001","ended_at":null,"flags":[],"id":1,"kind":"job","labels":[],"loop":{"interval_s":0,"max_attempts":1},"needs":{"slots":1},"not_before":null,"number":null,"sandbox":"process","schedule":null,"state":"running","supervision":{"beat_s":null,"expect_s":null}}],["runs",[1],{"attempt":1,"branch":null,"cost":{},"expect_until":1700000067000,"fence":null,"finished_at":null,"footprint":{},"harness":{},"holder":"alpha","id":1,"node":"n1","outcome":null,"pgid":null,"phase":"started","phase_at":1700000007000,"reason":null,"sandbox":"process","started_at":1700000007000,"task":1}],["meta",["next:reservations"],{"key":"next:reservations","value":2}],["reservations",[1],{"amount":1,"from_at":1700000007000,"holder":"alpha","id":1,"node":"n1","released_at":null,"resource":"slots","run":1,"task":1,"until_at":null,"why":null}]]');
INSERT INTO "journal" VALUES(8,1700000008000,'alpha','cli','run.phase','run:1','{"phase":"running","run":1,"task":1}','[["runs",[1],{"attempt":1,"branch":null,"cost":{},"expect_until":1700000068000,"fence":null,"finished_at":null,"footprint":{},"harness":{},"holder":"alpha","id":1,"node":"n1","outcome":null,"pgid":null,"phase":"running","phase_at":1700000008000,"reason":null,"sandbox":"process","started_at":1700000007000,"task":1}]]');
INSERT INTO "journal" VALUES(9,1700000009000,'alpha','cli','run.finish','run:1','{"outcome":"done","released":[1],"run":1,"task":1}','[["runs",[1],{"attempt":1,"branch":null,"cost":{},"expect_until":1700000068000,"fence":null,"finished_at":1700000009000,"footprint":{},"harness":{},"holder":"alpha","id":1,"node":"n1","outcome":"done","pgid":null,"phase":"running","phase_at":1700000008000,"reason":"finished","sandbox":"process","started_at":1700000007000,"task":1}],["reservations",[1],{"amount":1,"from_at":1700000007000,"holder":"alpha","id":1,"node":"n1","released_at":1700000009000,"resource":"slots","run":1,"task":1,"until_at":null,"why":"finished"}],["tasks",[1],{"attempts":1,"created_at":1700000006000,"effort":"0001","ended_at":null,"flags":[],"id":1,"kind":"job","labels":[],"loop":{"interval_s":0,"max_attempts":1},"needs":{"slots":1},"not_before":null,"number":null,"sandbox":"process","schedule":null,"state":"open","supervision":{"beat_s":null,"expect_s":null}}]]');
INSERT INTO "journal" VALUES(10,1700000010000,'alpha','cli','task.file','task:2','{"effort":"0001","id":2,"kind":"job","number":null}','[["meta",["next:tasks"],{"key":"next:tasks","value":3}],["tasks",[2],{"attempts":0,"created_at":1700000010000,"effort":"0001","ended_at":null,"flags":[],"id":2,"kind":"job","labels":[],"loop":{"interval_s":0,"max_attempts":1},"needs":{"slots":1},"not_before":null,"number":null,"sandbox":"process","schedule":null,"state":"open","supervision":{"beat_s":null,"expect_s":null}}]]');
INSERT INTO "journal" VALUES(11,1700000011000,'alpha','cli','run.start','run:2','{"attempt":1,"fence":null,"holder":"alpha","node":"n1","reservations":[2],"run":2,"task":2}','[["meta",["next:runs"],{"key":"next:runs","value":3}],["tasks",[2],{"attempts":1,"created_at":1700000010000,"effort":"0001","ended_at":null,"flags":[],"id":2,"kind":"job","labels":[],"loop":{"interval_s":0,"max_attempts":1},"needs":{"slots":1},"not_before":null,"number":null,"sandbox":"process","schedule":null,"state":"running","supervision":{"beat_s":null,"expect_s":null}}],["runs",[2],{"attempt":1,"branch":null,"cost":{},"expect_until":1700000071000,"fence":null,"finished_at":null,"footprint":{},"harness":{},"holder":"alpha","id":2,"node":"n1","outcome":null,"pgid":null,"phase":"started","phase_at":1700000011000,"reason":null,"sandbox":"process","started_at":1700000011000,"task":2}],["meta",["next:reservations"],{"key":"next:reservations","value":3}],["reservations",[2],{"amount":1,"from_at":1700000011000,"holder":"alpha","id":2,"node":"n1","released_at":null,"resource":"slots","run":2,"task":2,"until_at":null,"why":null}]]');
INSERT INTO "journal" VALUES(12,1700000012000,'alpha','cli','run.phase','run:2','{"phase":"running","run":2,"task":2}','[["runs",[2],{"attempt":1,"branch":null,"cost":{},"expect_until":1700000072000,"fence":null,"finished_at":null,"footprint":{},"harness":{},"holder":"alpha","id":2,"node":"n1","outcome":null,"pgid":null,"phase":"running","phase_at":1700000012000,"reason":null,"sandbox":"process","started_at":1700000011000,"task":2}]]');
CREATE TABLE "leases" (
  "id" INTEGER NOT NULL,
  "number" INTEGER NOT NULL,
  "holder" TEXT NOT NULL,
  "role" TEXT,
  "fence" INTEGER NOT NULL,
  "branch" TEXT NOT NULL,
  "machine" TEXT NOT NULL,
  "granted_at" INTEGER NOT NULL,
  "renewed_at" INTEGER NOT NULL,
  "paused_until" INTEGER,
  "expires_at" INTEGER,
  "released_at" INTEGER,
  "released_by" TEXT,
  "why" TEXT,
  PRIMARY KEY ("id")
) STRICT;
CREATE TABLE "left" (
  "import" INTEGER NOT NULL,
  "table" TEXT NOT NULL,
  "legacy_ref" TEXT NOT NULL,
  "legacy_key" TEXT CHECK (json_valid("legacy_key")),
  "count" INTEGER NOT NULL,
  "why" TEXT NOT NULL,
  "at" INTEGER NOT NULL,
  PRIMARY KEY ("import", "table", "legacy_ref")
) STRICT;
CREATE TABLE "merges" (
  "id" INTEGER NOT NULL,
  "change" INTEGER NOT NULL,
  "sha" TEXT NOT NULL,
  "commit" TEXT,
  "by" TEXT NOT NULL,
  "rule" TEXT NOT NULL,
  "at" INTEGER NOT NULL,
  PRIMARY KEY ("id")
) STRICT;
CREATE TABLE "meta" (
  "key" TEXT NOT NULL,
  "value" TEXT NOT NULL CHECK (json_valid("value")),
  PRIMARY KEY ("key")
) STRICT;
INSERT INTO "meta" VALUES('next:efforts','2');
INSERT INTO "meta" VALUES('next:items','2');
INSERT INTO "meta" VALUES('next:comments','2');
INSERT INTO "meta" VALUES('next:tasks','3');
INSERT INTO "meta" VALUES('next:runs','3');
INSERT INTO "meta" VALUES('next:reservations','3');
CREATE TABLE "nodes" (
  "name" TEXT NOT NULL,
  "drivers" TEXT NOT NULL CHECK (json_valid("drivers")),
  "labels" TEXT NOT NULL CHECK (json_valid("labels")),
  "declared_at" INTEGER NOT NULL,
  "retired_at" INTEGER,
  PRIMARY KEY ("name")
) STRICT;
INSERT INTO "nodes" VALUES('n1','["process"]','["zone:a"]',1700000004000,NULL);
CREATE TABLE "pulled" (
  "import" INTEGER NOT NULL,
  "table" TEXT NOT NULL,
  "legacy_ref" TEXT NOT NULL,
  "record" TEXT NOT NULL,
  "record_key" TEXT NOT NULL CHECK (json_valid("record_key")),
  "at" INTEGER NOT NULL,
  PRIMARY KEY ("import", "table", "legacy_ref")
) STRICT;
CREATE TABLE "questions" (
  "id" INTEGER NOT NULL,
  "number" INTEGER NOT NULL,
  "run" INTEGER,
  "who" TEXT NOT NULL,
  "kind" TEXT NOT NULL,
  "text" TEXT NOT NULL,
  "asked_at" INTEGER NOT NULL,
  "answered_at" INTEGER,
  "answered_by" TEXT,
  "answer" INTEGER,
  PRIMARY KEY ("id")
) STRICT;
CREATE TABLE "readings" (
  "id" INTEGER NOT NULL,
  "source" TEXT NOT NULL,
  "kind" TEXT NOT NULL,
  "value" TEXT NOT NULL CHECK (json_valid("value")),
  "at" INTEGER NOT NULL,
  PRIMARY KEY ("id")
) STRICT;
CREATE TABLE "reservations" (
  "id" INTEGER NOT NULL,
  "node" TEXT NOT NULL,
  "resource" TEXT NOT NULL,
  "amount" INTEGER NOT NULL,
  "task" INTEGER,
  "run" INTEGER,
  "holder" TEXT NOT NULL,
  "from_at" INTEGER NOT NULL,
  "until_at" INTEGER,
  "released_at" INTEGER,
  "why" TEXT,
  PRIMARY KEY ("id")
) STRICT;
INSERT INTO "reservations" VALUES(1,'n1','slots',1,1,1,'alpha',1700000007000,NULL,1700000009000,'finished');
INSERT INTO "reservations" VALUES(2,'n1','slots',1,2,2,'alpha',1700000011000,NULL,NULL,NULL);
CREATE TABLE "reviews" (
  "id" INTEGER NOT NULL,
  "target" INTEGER NOT NULL,
  "round" INTEGER NOT NULL,
  "kind" TEXT NOT NULL,
  "item" INTEGER,
  "author" TEXT NOT NULL,
  "heads" TEXT NOT NULL CHECK (json_valid("heads")),
  "pages" TEXT NOT NULL CHECK (json_valid("pages")),
  "filed_by" TEXT,
  "reviewer" TEXT,
  "verdict" TEXT,
  "findings" TEXT NOT NULL CHECK (json_valid("findings")),
  "filed_at" INTEGER NOT NULL,
  "verdict_at" INTEGER,
  "read_at" INTEGER,
  PRIMARY KEY ("id")
) STRICT;
CREATE TABLE "run_text" (
  "run" INTEGER NOT NULL,
  "result" TEXT NOT NULL,
  PRIMARY KEY ("run")
) STRICT;
CREATE TABLE "runs" (
  "id" INTEGER NOT NULL,
  "task" INTEGER NOT NULL,
  "attempt" INTEGER NOT NULL,
  "holder" TEXT NOT NULL,
  "fence" INTEGER,
  "node" TEXT NOT NULL,
  "sandbox" TEXT NOT NULL,
  "branch" TEXT,
  "pgid" INTEGER,
  "phase" TEXT NOT NULL,
  "phase_at" INTEGER NOT NULL,
  "expect_until" INTEGER,
  "harness" TEXT NOT NULL CHECK (json_valid("harness")),
  "footprint" TEXT NOT NULL CHECK (json_valid("footprint")),
  "cost" TEXT NOT NULL CHECK (json_valid("cost")),
  "outcome" TEXT,
  "reason" TEXT,
  "started_at" INTEGER NOT NULL,
  "finished_at" INTEGER,
  PRIMARY KEY ("id")
) STRICT;
INSERT INTO "runs" VALUES(1,1,1,'alpha',NULL,'n1','process',NULL,NULL,'running',1700000008000,1700000068000,'{}','{}','{}','done','finished',1700000007000,1700000009000);
INSERT INTO "runs" VALUES(2,2,1,'alpha',NULL,'n1','process',NULL,NULL,'running',1700000012000,1700000072000,'{}','{}','{}',NULL,NULL,1700000011000,NULL);
CREATE TABLE "statements" (
  "id" INTEGER NOT NULL,
  "type" TEXT NOT NULL,
  "effort" TEXT,
  "number" INTEGER,
  "text" TEXT NOT NULL,
  "who" TEXT NOT NULL,
  "at" INTEGER NOT NULL,
  "where" TEXT,
  "supersedes" INTEGER,
  PRIMARY KEY ("id")
) STRICT;
CREATE TABLE "tasks" (
  "id" INTEGER NOT NULL,
  "number" INTEGER,
  "effort" TEXT,
  "kind" TEXT NOT NULL,
  "labels" TEXT NOT NULL CHECK (json_valid("labels")),
  "flags" TEXT NOT NULL CHECK (json_valid("flags")),
  "needs" TEXT NOT NULL CHECK (json_valid("needs")),
  "sandbox" TEXT NOT NULL,
  "schedule" TEXT,
  "not_before" INTEGER,
  "supervision" TEXT NOT NULL CHECK (json_valid("supervision")),
  "loop" TEXT NOT NULL CHECK (json_valid("loop")),
  "attempts" INTEGER NOT NULL,
  "state" TEXT NOT NULL,
  "created_at" INTEGER NOT NULL,
  "ended_at" INTEGER,
  PRIMARY KEY ("id")
) STRICT;
INSERT INTO "tasks" VALUES(1,NULL,'0001','job','[]','[]','{"slots":1}','process',NULL,NULL,'{"beat_s":null,"expect_s":null}','{"interval_s":0,"max_attempts":1}',1,'open',1700000006000,NULL);
INSERT INTO "tasks" VALUES(2,NULL,'0001','job','[]','[]','{"slots":1}','process',NULL,NULL,'{"beat_s":null,"expect_s":null}','{"interval_s":0,"max_attempts":1}',1,'running',1700000010000,NULL);
CREATE TRIGGER "meta_no_delete" BEFORE DELETE ON "meta" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "efforts_by_standing" ON "efforts"("standing");
CREATE TRIGGER "efforts_no_delete" BEFORE DELETE ON "efforts" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "effort_links_by_effort" ON "effort_links"("effort");
CREATE INDEX "effort_links_by_target" ON "effort_links"("target");
CREATE TRIGGER "effort_links_no_delete" BEFORE DELETE ON "effort_links" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "items_by_state" ON "items"("state");
CREATE INDEX "items_by_kind" ON "items"("kind");
CREATE INDEX "items_by_role" ON "items"("role");
CREATE INDEX "items_by_parent" ON "items"("parent");
CREATE TRIGGER "items_no_delete" BEFORE DELETE ON "items" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE TRIGGER "item_text_no_delete" BEFORE DELETE ON "item_text" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "deps_by_number" ON "deps"("number");
CREATE INDEX "deps_by_blocker" ON "deps"("blocker");
CREATE INDEX "deps_by_change" ON "deps"("change");
CREATE TRIGGER "deps_no_delete" BEFORE DELETE ON "deps" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "leases_by_number" ON "leases"("number");
CREATE INDEX "leases_by_holder" ON "leases"("holder");
CREATE TRIGGER "leases_no_delete" BEFORE DELETE ON "leases" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "tasks_by_number" ON "tasks"("number");
CREATE INDEX "tasks_by_state" ON "tasks"("state");
CREATE INDEX "tasks_by_kind" ON "tasks"("kind");
CREATE INDEX "tasks_by_effort" ON "tasks"("effort");
CREATE TRIGGER "tasks_no_delete" BEFORE DELETE ON "tasks" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE TRIGGER "nodes_no_delete" BEFORE DELETE ON "nodes" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "capacity_by_node" ON "capacity"("node");
CREATE TRIGGER "capacity_no_delete" BEFORE DELETE ON "capacity" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "reservations_by_node" ON "reservations"("node");
CREATE INDEX "reservations_by_task" ON "reservations"("task");
CREATE INDEX "reservations_by_run" ON "reservations"("run");
CREATE TRIGGER "reservations_no_delete" BEFORE DELETE ON "reservations" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "runs_by_task" ON "runs"("task");
CREATE INDEX "runs_by_holder" ON "runs"("holder");
CREATE INDEX "runs_by_node" ON "runs"("node");
CREATE TRIGGER "runs_no_delete" BEFORE DELETE ON "runs" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE TRIGGER "run_text_no_delete" BEFORE DELETE ON "run_text" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "changes_by_number" ON "changes"("number");
CREATE INDEX "changes_by_repo" ON "changes"("repo");
CREATE INDEX "changes_by_branch" ON "changes"("branch");
CREATE TRIGGER "changes_no_delete" BEFORE DELETE ON "changes" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE TRIGGER "change_text_no_delete" BEFORE DELETE ON "change_text" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "checks_by_change" ON "checks"("change");
CREATE TRIGGER "checks_no_delete" BEFORE DELETE ON "checks" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "reviews_by_target" ON "reviews"("target");
CREATE INDEX "reviews_by_item" ON "reviews"("item");
CREATE TRIGGER "reviews_no_delete" BEFORE DELETE ON "reviews" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "merges_by_change" ON "merges"("change");
CREATE TRIGGER "merges_no_delete" BEFORE DELETE ON "merges" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "comments_by_number" ON "comments"("number");
CREATE INDEX "comments_by_change" ON "comments"("change");
CREATE TRIGGER "comments_no_delete" BEFORE DELETE ON "comments" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "questions_by_number" ON "questions"("number");
CREATE TRIGGER "questions_no_delete" BEFORE DELETE ON "questions" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "statements_by_effort" ON "statements"("effort");
CREATE INDEX "statements_by_number" ON "statements"("number");
CREATE INDEX "statements_by_type" ON "statements"("type");
CREATE TRIGGER "statements_no_delete" BEFORE DELETE ON "statements" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "holds_by_number" ON "holds"("number");
CREATE INDEX "holds_by_who" ON "holds"("who");
CREATE TRIGGER "holds_no_delete" BEFORE DELETE ON "holds" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "readings_by_source" ON "readings"("source");
CREATE INDEX "readings_by_kind" ON "readings"("kind");
CREATE TRIGGER "readings_no_delete" BEFORE DELETE ON "readings" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE TRIGGER "imports_no_delete" BEFORE DELETE ON "imports" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "pulled_by_import" ON "pulled"("import");
CREATE INDEX "pulled_by_record" ON "pulled"("record");
CREATE TRIGGER "pulled_no_delete" BEFORE DELETE ON "pulled" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE INDEX "left_by_import" ON "left"("import");
CREATE TRIGGER "left_no_delete" BEFORE DELETE ON "left" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE TRIGGER "beats_no_delete" BEFORE DELETE ON "beats" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE TRIGGER "cursors_no_delete" BEFORE DELETE ON "cursors" BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END;
CREATE UNIQUE INDEX "leases_one_live" ON "leases"("number") WHERE released_at IS NULL;
CREATE UNIQUE INDEX "changes_one_open" ON "changes"("repo", "branch") WHERE merged_at IS NULL AND dropped_at IS NULL;
CREATE INDEX journal_by_subject ON journal(subject, seq);
CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal BEGIN SELECT RAISE(ABORT, 'the journal is append-only'); END;
CREATE TRIGGER journal_no_delete BEFORE DELETE ON journal BEGIN SELECT RAISE(ABORT, 'the journal is append-only'); END;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('journal',12);
COMMIT;
