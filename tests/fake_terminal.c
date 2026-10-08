/* 测试用的假 terminal64.exe（Linux）：
 * - 把 /config:xxx.ini 的内容原样复制到当前目录 fake_seen_<n>.ini（证明启动时读到了，之后程序会删掉原文件）
 * - 如果配置里有 [StartUp] Expert=...，在 Logs/YYYYMMDD.log（UTF-16LE）里写一行 EA 加载成功，模仿 MT5 日志
 * - 读取 MQL5/Profiles/Charts/Default 下的 .chr（UTF-16），里面每个 <expert> 段落也写一行“加载成功”（模仿 MT5 按图表配置加载 EA）
 * - 模仿 MT5 自动更新（LiveUpdate）：启动时如果当前目录有 fake_update_on_start，或运行中出现 fake_update_later，
 *   就删掉这个标记文件、派生一个用完全相同参数重新启动的新进程（新 PID），自己退出
 * - 然后一直运行，直到被结束
 * 编译：cc -O2 -o terminal64.exe tests/fake_terminal.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/stat.h>
#include <dirent.h>
#include <signal.h>

static void field(const char *txt, const char *key, char *out, size_t n) {
    out[0] = 0;
    const char *p = strstr(txt, key);
    if (!p) return;
    p += strlen(key);
    size_t i = 0;
    while (*p && *p != '\r' && *p != '\n' && i + 1 < n) out[i++] = *p++;
    out[i] = 0;
}

static void put16(FILE *f, const char *s) {
    for (; *s; s++) { fputc((unsigned char)*s, f); fputc(0, f); }
}

static void log_line(const char *msg) {
    mkdir("Logs", 0755);
    char path[64];
    time_t t = time(NULL);
    struct tm *tm = localtime(&t);
    strftime(path, sizeof path, "Logs/%Y%m%d.log", tm);
    int fresh = access(path, F_OK) != 0;
    FILE *lg = fopen(path, "ab");
    if (!lg) return;
    if (fresh) { fputc(0xFF, lg); fputc(0xFE, lg); }
    put16(lg, msg);
    fclose(lg);
}

static void load_profile_experts(void) {
    const char *dir = "MQL5/Profiles/Charts/Default";
    DIR *d = opendir(dir);
    if (!d) return;
    struct dirent *e;
    while ((e = readdir(d))) {
        size_t L = strlen(e->d_name);
        if (L < 5 || strcmp(e->d_name + L - 4, ".chr") != 0) continue;
        char path[512];
        snprintf(path, sizeof path, "%s/%s", dir, e->d_name);
        FILE *f = fopen(path, "rb");
        if (!f) continue;
        static unsigned char raw[262144];
        size_t n = fread(raw, 1, sizeof raw, f);
        fclose(f);
        static char txt[131072];
        size_t j = 0, s = (n >= 2 && raw[0] == 0xFF && raw[1] == 0xFE) ? 2 : 0;
        for (size_t i = s; i + 1 < n && j + 1 < sizeof txt; i += 2) txt[j++] = (char)raw[i];
        txt[j] = 0;
        char sym[64];
        field(txt, "symbol=", sym, sizeof sym);
        const char *p = txt;
        while ((p = strstr(p, "<expert>"))) {
            char name[256];
            field(p, "name=", name, sizeof name);
            char line[512];
            snprintf(line, sizeof line, "KP\t0\t12:00:01.000\tExperts\texpert %s (%s,M15) loaded successfully\r\n", name, sym);
            log_line(line);
            p += 8;
        }
    }
    closedir(d);
}

static void fake_update(char **argv) {
    log_line("MG\t0\t12:00:02.000\tLiveUpdate\tstart terminal64.exe /update (fake)\r\n");
    pid_t pid = fork();
    if (pid == 0) {
        setsid();
        pid_t g = fork();          /* 孙进程：父进程是 init，模仿更新程序启动的新终端 */
        if (g != 0) _exit(0);
        sleep(2);
        execv(argv[0], argv);
        _exit(1);
    }
    exit(0);
}

int main(int argc, char **argv) {
    signal(SIGCHLD, SIG_IGN);
    if (access("fake_update_on_start", F_OK) == 0) { unlink("fake_update_on_start"); fake_update(argv); }
    const char *cfg = NULL;
    for (int i = 1; i < argc; i++)
        if (strncmp(argv[i], "/config:", 8) == 0) cfg = argv[i] + 8;
    if (cfg) {
        FILE *f = fopen(cfg, "rb");
        if (f) {
            static unsigned char raw[65536];
            size_t n = fread(raw, 1, sizeof raw, f);
            fclose(f);
            char name[64];
            int k = 0;
            for (;; k++) { snprintf(name, sizeof name, "fake_seen_%d.ini", k); if (access(name, F_OK) != 0) break; }
            FILE *o = fopen(name, "wb");
            if (o) { fwrite(raw, 1, n, o); fclose(o); }
            static char txt[32768];
            size_t j = 0, s = (n >= 2 && raw[0] == 0xFF && raw[1] == 0xFE) ? 2 : 0;
            int utf16 = (n > s + 1 && raw[s + 1] == 0);
            for (size_t i = s; i < n && j + 1 < sizeof txt; i += utf16 ? 2 : 1) txt[j++] = (char)raw[i];
            txt[j] = 0;
            char expert[256], sym[64], per[16];
            field(txt, "Expert=", expert, sizeof expert);
            field(txt, "Symbol=", sym, sizeof sym);
            field(txt, "Period=", per, sizeof per);
            if (expert[0]) {
                const char *nm = strrchr(expert, '\\');
                nm = nm ? nm + 1 : expert;
                mkdir("Logs", 0755);
                char path[64];
                time_t t = time(NULL);
                struct tm *tm = localtime(&t);
                strftime(path, sizeof path, "Logs/%Y%m%d.log", tm);
                int fresh = access(path, F_OK) != 0;
                FILE *lg = fopen(path, "ab");
                if (lg) {
                    if (fresh) { fputc(0xFF, lg); fputc(0xFE, lg); }
                    char line[512];
                    snprintf(line, sizeof line, "KO\t0\t12:00:00.000\tExperts\texpert %s (%s,%s) loaded successfully\r\n", nm, sym, per);
                    put16(lg, line);
                    fclose(lg);
                }
            }
        }
    }
    load_profile_experts();
    for (;;) {
        sleep(1);
        if (access("fake_update_later", F_OK) == 0) { unlink("fake_update_later"); fake_update(argv); }
    }
}
