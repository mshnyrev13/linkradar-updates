#define UNICODE
#define _UNICODE
#include <windows.h>

DWORD _tls_index;
static wchar_t root[32768], python[32768], script[32768], data[32768], command[32768];
static STARTUPINFOW startup;
static PROCESS_INFORMATION process;
static BOOL append(wchar_t *dst, const wchar_t *src) {
    unsigned int n=0;
    while(dst[n]) n++;
    while(*src) { if(n>=32767) return FALSE; dst[n++]=*src++; }
    dst[n]=0;
    return TRUE;
}
static DWORD launch(void) {
    if (!GetModuleFileNameW(NULL,root,32768)) return 1;
    wchar_t *slash=NULL;
    for(wchar_t *p=root;*p;p++) if(*p==L'\\') slash=p;
    if(!slash) return 1;
    *slash=0;
    if(!append(python,root)||!append(python,L"\\runtime\\pythonw.exe")||
       !append(script,root)||!append(script,L"\\app\\desktop.py")) return 1;
    if(GetFileAttributesW(python)==INVALID_FILE_ATTRIBUTES||GetFileAttributesW(script)==INVALID_FILE_ATTRIBUTES) {
        MessageBoxW(NULL,L"Extract the entire ZIP before launching the application.",L"LinkRadar",MB_ICONERROR);
        return 1;
    }
    if(!append(command,L"\"")||!append(command,python)||!append(command,L"\" \"")||
       !append(command,script)||!append(command,L"\"")) return 1;
    startup.cb=sizeof(startup);
    if(!CreateProcessW(python,command,NULL,NULL,FALSE,CREATE_NO_WINDOW,NULL,root,&startup,&process)) {
        MessageBoxW(NULL,L"Unable to launch LinkRadar.",L"LinkRadar",MB_ICONERROR);
        return 1;
    }
    CloseHandle(process.hThread);
    WaitForSingleObject(process.hProcess,INFINITE);
    DWORD exitcode=0;GetExitCodeProcess(process.hProcess,&exitcode);
    CloseHandle(process.hProcess);
    return exitcode;
}
void WINAPI entry(void) { ExitProcess(launch()); }
