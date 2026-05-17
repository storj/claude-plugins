---
name: dbx
description: Use when working with DBX database schema definitions, generating Go database bindings, creating models, or writing CRUD operations. DBX is a code generation tool that creates Go code for Postgres, CockroachDB, Spanner, and SQLite databases.
argument-hint: "[create|read|update|delete|model|schema|help]"
---

# DBX Database Code Generator

DBX generates database schemas and Go code for interacting with databases. It supports Postgres (pgx), CockroachDB (pgxcockroach), Spanner, and SQLite3 dialects.

## Installation

```bash
go install storj.io/dbx@latest
```

## CLI Commands

```bash
# Generate Go code
dbx golang [-p package] [-d dialect] [-t templates] [--userdata] [-i include.dbx] schema.dbx outdir

# Generate SQL schema
dbx schema [-d dialect] [-i include.dbx] schema.dbx outdir

# Format dbx file (reads stdin, writes stdout)
dbx format < input.dbx > output.dbx
```

### Dialect Options

- `pgx` (default) - PostgreSQL via pgx driver
- `pgxcockroach` - CockroachDB via pgx driver
- `spanner` - Google Cloud Spanner via go-sql-spanner
- `sqlite3` - SQLite3

### Go Generate Directive

```go
//go:generate dbx golang -d pgx -d pgxcockroach -d spanner schema.dbx .
//go:generate dbx schema -d pgx -d pgxcockroach -d spanner schema.dbx .
```

## DBX File Syntax

DBX uses a tuple/list grammar. Lists are parentheses-enclosed comma-separated tuples. Newlines auto-insert commas.

## Model Definition

```dbx
model <name> (
    // Optional: custom table name
    table <table_name>

    // Required: primary key (single or composite)
    key <field_names>

    // Optional: unique constraints
    unique <field_names>

    // Optional: indexes
    index (
        name <index_name>
        fields <field_names>
        unique              // optional
        storing <fields>    // optional: include columns (covering index)
        where <condition>   // optional: partial index
    )

    // Fields
    field <name> <type> ( attributes )

    // Foreign key relations
    field <name> <model>.<field> <relation> ( attributes )
)
```

### Field Types

| Type | Go Type | Description |
|------|---------|-------------|
| `serial` | `int` | Auto-incrementing integer |
| `serial64` | `int64` | Auto-incrementing 64-bit integer |
| `int` | `int` | 32-bit integer |
| `int64` | `int64` | 64-bit integer |
| `uint` | `uint` | Unsigned 32-bit integer |
| `uint64` | `uint64` | Unsigned 64-bit integer |
| `bool` | `bool` | Boolean |
| `text` | `string` | Text/varchar |
| `timestamp` | `time.Time` | Timestamp with timezone |
| `utimestamp` | `time.Time` | Timestamp without timezone (UTC) |
| `float` | `float32` | 32-bit float |
| `float64` | `float64` | 64-bit float |
| `blob` | `[]byte` | Binary data |
| `json` | `[]byte` | JSON data (uses JSON/JSONB SQL type) |
| `date` | `time.Time` | Date only |

### Field Attributes

| Attribute | Description |
|-----------|-------------|
| `column <name>` | Custom column name |
| `nullable` | Field can be NULL |
| `updatable` | Field can be updated |
| `autoinsert` | Auto-populate on insert (timestamps get current time) |
| `autoupdate` | Auto-populate on update (timestamps get current time) |
| `length <n>` | Max text length |
| `default <value>` | Default value (numbers, strings, `"epoch"` for timestamps, `"{}"` for JSON) |

### Foreign Key Relations

| Relation | Behavior on Delete |
|----------|-------------------|
| `cascade` | Delete this row when related row is deleted |
| `restrict` | Prevent deletion of related row |
| `setnull` | Set field to NULL (requires `nullable`) |

## CRUD Operations

### Create

```dbx
create <model> (
    raw        // expose all fields, including autoinsert ones
    noreturn   // don't return the created row
    replace    // upsert behavior (INSERT OR REPLACE)
    suffix <parts>  // custom method name suffix
)
```

### Read

```dbx
read <views> (
    select <model_or_fields>
    where <expr> <op> <expr>
    join <model.field> = <model.field>
    orderby <asc|desc> <model.field>
    groupby <model.field>
    suffix <parts>
)
```

**Views (for all reads):**
- `count` - returns count of results
- `has` - returns boolean if results exist
- `first` - returns first result or nil
- `scalar` - returns single result, nil, or error if multiple
- `one` - returns single result or error

**Views (for non-distinct reads only):**
- `all` - returns all results as slice
- `limitoffset` - paginated results with limit/offset
- `paged` - forward cursor pagination

**Where Expressions:**
```dbx
where model.field = ?              // placeholder parameter
where model.field = null           // NULL check
where model.field = "literal"      // string literal
where model.field < 30             // numeric literal
where model.field = true           // boolean literal
where model.field = other.field    // field comparison
where lower(model.field) = ?       // function call (lower supported)
where ( a.x = ?, a.y = ?, a.z = ? ) // OR grouping
```

**Operators:** `=`, `!=`, `<`, `<=`, `>`, `>=`

### Update

```dbx
update <model> (
    where <model.field> <op> <expr>
    join <model.field> = <model.field>
    noreturn   // don't return the updated row
    suffix <parts>
)
```

Updates only fields marked `updatable`. Requires unique identification via where/join.

### Delete

```dbx
delete <model> (
    where <model.field> <op> <expr>
    join <model.field> = <model.field>
    suffix <parts>
)
```

## Common Patterns

### Basic User Model

```dbx
model user (
    key    pk
    unique id
    unique email

    field pk         serial64
    field created_at timestamp ( autoinsert )
    field updated_at timestamp ( autoinsert, autoupdate )
    field id         text
    field email      text
    field name       text      ( updatable )
)

create user ( )
read one ( select user, where user.pk = ? )
read one ( select user, where user.id = ? )
update user ( where user.pk = ? )
delete user ( where user.pk = ? )
```

### Many-to-Many Relationship

```dbx
model user (
    key pk
    field pk serial64
)

model role (
    key pk
    field pk   serial64
    field name text
)

model user_role (
    key   user_pk role_pk
    field user_pk user.pk cascade
    field role_pk role.pk cascade
)

create user_role ( )

read all (
    select role
    join role.pk = user_role.role_pk
    join user_role.user_pk = user.pk
    where user.pk = ?
)
```

### Nullable Foreign Key with Setnull

```dbx
model post (
    key pk
    field pk        serial64
    field author_pk user.pk setnull ( nullable )
    field content   text
)
```

### Composite Primary Key

```dbx
model event_log (
    key timestamp source_id

    field timestamp  utimestamp
    field source_id  blob
    field event_type uint
    field payload    json
)

create event_log ( noreturn )

read paged (
    select event_log
    where event_log.source_id = ?
)
```

### Partial Index

```dbx
model task (
    key pk
    index (
        fields status
        where task.status != "completed"
    )

    field pk     serial64
    field status text ( updatable )
)
```

### Key-Value Store with Replace

```dbx
model kv (
    key key
    field key text
    field val text
)

create kv ( replace, noreturn )
read one ( select kv, where kv.key = ? )
```

## Generated Code Usage

```go
// Open database
db, err := mypackage.Open("pgx", "postgres://...")

// Create
user, err := db.Create_User(ctx,
    mypackage.User_Id("uuid-here"),
    mypackage.User_Email("user@example.com"),
    mypackage.User_Name("John Doe"))

// Read
user, err := db.Get_User_By_Pk(ctx,
    mypackage.User_Pk(123))

// Update
user, err := db.Update_User_By_Pk(ctx,
    mypackage.User_Pk(123),
    mypackage.User_Update_Fields{
        Name: mypackage.User_Name("Jane Doe"),
    })

// Delete
deleted, err := db.Delete_User_By_Pk(ctx,
    mypackage.User_Pk(123))

// Transaction
tx, err := db.Open(ctx)
defer tx.Rollback()
// ... operations on tx ...
err = tx.Commit()
```

## Customization Hooks

```go
// Wrap all errors
mypackage.WrapErr = func(err *mypackage.Error) error {
    return fmt.Errorf("db error: %w", err)
}

// Log all SQL
mypackage.Logger = func(format string, args ...any) {
    log.Printf(format, args...)
}

// Mock time in tests
db.Hooks.Now = func() time.Time {
    return fixedTime
}
```

## Common Errors

- **"no field X defined on model Y"** - Check field name spelling in key/unique/where clauses
- **"no updatable fields"** - Add `updatable` attribute to fields you want to update
- **"must specify some field references"** - `key` clause requires field names
- **Cyclic foreign key** - Model A references B which references A; restructure models

## Tips

1. Always define `key` for every model
2. Use `serial64` for primary keys unless you have a specific reason
3. Mark timestamp fields with `autoinsert` and/or `autoupdate`
4. Only mark fields `updatable` that should be modifiable after creation
5. Use `noreturn` on creates when you don't need the result (batch inserts)
6. Use `replace` for upsert/idempotent operations
7. Combine `nullable` with `setnull` foreign keys for optional relationships
8. Use `suffix` to disambiguate methods with the same signature
