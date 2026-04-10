@.str.0 = private unnamed_addr constant [13 x i8] c"hello, world\00"

declare i32 @puts(ptr)

define i32 @main() {
entry:
  %t1 = call i32 @puts(ptr @.str.0)
  ret i32 0
}

