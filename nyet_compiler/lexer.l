%{
#include <string>
#include "node.h"
#include "parser.hpp"
#define SAVE_TOKEN yylval.string = new std::string(yytext, yyleng)
#define TOKEN(t) (yylval.token = t)
extern "C" int yywrap() { }
%}

%option noyywrap

%%


[a-zA-Z_][a-zA-Z0-9_]*  SAVE_TOKEN; return TID;
[0-9]+.[0-9]*           SAVE_TOKEN; return TDOUBLE;
[0-9]+                  SAVE_TOKEN; return TINTEGER;
"([^\"]*)"              SAVE_TOKEN; { yylval.value = strdup(yytext + 1); yylval.value[strlen(yylval.value) - 1] = '\0'; return TSTRING; }
"int"                   return TINTEGER;
"string"                return TSTRING;
"double"                return TDOUBLE;


"let\b"                 return TOKEN(TLET);
"if\b"                  return TOKEN(TIF);
"fn\b"                  return TOKEN(TFN);
"return\b"              return TOKEN(TRETURN);
"out\b"                 return TOKEN(TOUT);
"in\b"                  return TOKEN(TIN);
"true\b"                return TOKEN(TTRUE);
"false\b"               return TOKEN(TFALSE);
"pass"                  return TOKEN(TPASS);




"=="                    return TOKEN(TCEQ);
"!="                    return TOKEN(TCNE);
"<"                     return TOKEN(TCLT);
"<="                    return TOKEN(TCLE);
">"                     return TOKEN(TCGT);
">="                    return TOKEN(TCGE);


"("                     return TOKEN(TLPAREN);
")"                     return TOKEN(TRPAREN);
"{"                     return TOKEN(TLBRACE);
"}"                     return TOKEN(TRBRACE);
","                     return TOKEN(TCOMMA);
"."                     return TOKEN(TDOT);
":"                     return TOKEN(TCOLON);


"+"                     return TOKEN(TPLUS);
"-"                     return TOKEN(TMINUS);
"*"                     return TOKEN(TMUL);
"/"                     return TOKEN(TDIV);

.                       printf("Unknown token!n"); yyterminate();


;.*               { /* ignore comments */ }
#\|[\s\S]*?\|#    { /* ignore block comments */ }

%%

int is_keyword(const char *value) {
    // Function to check if the given identifier is a keyword
    // Implement this function based on your list of keywords
}

int keyword_type(const char *value) {
    // Function to map keywords to their corresponding token types
    // Implement this function based on your list of keywords
}
