
%{
    #include "node.h"
    NBlock *programBlock; /* the top level root node of our final AST */

    extern int yylex();
    void yyerror(const char *s) { printf("ERROR: %sn", s); }
%}

/* Represents the many different ways we can access our data */
%union {
    Node *node;
    NBlock *block;
    NExpression *expr;
    NStatement *stmt;
    NIdentifier *ident;
    NVariableDeclaration *var_decl;
    std::vector<NVariableDeclaration*> *varvec;
    std::vector<NExpression*> *exprvec;
    std::string *string;
    int token;
}

/* Define our terminal symbols (tokens). This should
   match our tokens.l lex file. We also define the node type
   they represent.
 */
%token <string> TID TINTEGER TDOUBLE TSTRING
%token <token> TLET TIF TFN TRETURN TOUT TIN TTRUE TFALSE TPASS
%token <token> TCEQ TCNE TCLT TCLE TCGT TCGE
%token <token> TLPAREN TRPAREN TLBRACE TRBRACE TCOMMA TDOT TCOLON
%token <token> TPLUS TMINUS TMUL TDIV

/* Define the type of node our nonterminal symbols represent.
   The types refer to the %union declaration above. Ex: when
   we call an ident (defined by union type ident) we are really
   calling an (NIdentifier*). It makes the compiler happy.
 */
%type <ident> ident
%type <expr> numeric expr 
%type <varvec> func_decl_args_inner
%type <exprvec> call_args
%type <block> program stmts block
%type <stmt> stmt var_decl func_decl if_stmt
%type <token> comparison

/* Operator precedence for mathematical operators */
%left TLPAREN TFN TLET TCOLON
%left TPLUS TMINUS
%left TMUL TDIV
%right TCOMMA


%start program

%%


program : stmts { programBlock = $1; }
        ;

stmts : stmt { $$ = new NBlock(); $$->statements.push_back($<stmt>1); }
      | stmts stmt { $1->statements.push_back($<stmt>2); }
      ;

stmt : var_decl | func_decl | if_stmt | expr { $$ = new NExpressionStatement(*$1); }
     ;

block : TLPAREN stmts TRPAREN { $$ = $2; }
      | TLPAREN TRPAREN { $$ = new NBlock(); }
      ;
ident : TID { $$ = new NIdentifier(*$1); delete $1; }
var_decl : TLPAREN TLET ident TCOLON type func_decl_args_inner { $$ = new NVariableDeclaration(*$4, *$2, *$6); }

type : TINTEGER | TDOUBLE | TSTRING
     ;

func_decl : TLPAREN TFN ident TLPAREN func_decl_args_inner TRPAREN type block { $$ = new NFunctionDeclaration(*$3, *$6, *$8, *$10); delete $6; }

func_decl_args_inner : /*blank*/  { $$ = new VariableList(); }
                   | var_decl { $$ = new VariableList(); $$->push_back($<var_decl>1); }
                   | func_decl_args_inner TCOMMA var_decl { $1->push_back($<var_decl>3); }
                   ;

if_stmt : TLPAREN TIF expr expr expr TRPAREN { $$ = new NIfStatement(*$3, *$5, *$7); }

expr : var_decl { $$ = new NAssignment(*$1, *$3); }
     | if_stmt { $$ = new NIfStatement(*$3, *$5, nullptr); }
     | expr TCOMMA expr { $$ = new NBinaryOperator(*$1, TCOMMA, *$3); }
     | expr TPLUS expr { $$ = new NBinaryOperator(*$1, TPLUS, *$3); }
     | expr TMINUS expr { $$ = new NBinaryOperator(*$1, TMINUS, *$3); }
     | expr TMUL expr { $$ = new NBinaryOperator(*$1, TMUL, *$3); }
     | expr TDIV expr { $$ = new NBinaryOperator(*$1, TDIV, *$3); }
     | TLPAREN expr TRPAREN { $$ = $2; }
     | ident { $$ = *$1; }
     | numeric
     ;

call_args : /*blank*/  { $$ = new ExpressionList(); }
          | expr { $$ = new ExpressionList(); $$->push_back($1); }
          | call_args TCOMMA expr  { $1->push_back($3); }
          ;

numeric : TINTEGER { $$ = new NInteger(atol($1->c_str())); delete $1; }
        | TDOUBLE { $$ = new NDouble(atof($1->c_str())); delete $1; }
        ;

comparison : TCEQ | TCNE | TCLT | TCLE | TCGT | TCGE
           | TPLUS | TMINUS | TMUL | TDIV
           ;
%%
