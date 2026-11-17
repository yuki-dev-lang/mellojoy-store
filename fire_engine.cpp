// mellojoy_fire_engine v2 — 常駐型・自己検知・一斉発火エンジン (A案)
//
// v2 での変更（発火遅延の修正）:
//   旧版は「温め(warm)完了 → 検知ループ → 発火」が1本の直列処理だったため、
//     - 起動直後の温めがプロキシ遅延で長引くと、検知も発火も始まらない
//     - 検知GETが応答待ち(最大 timeout_ms)の間は発火時刻をチェックできない
//     - 定期再温め(warm_interval)が発火直前に重なると join で待たされる
//   の3つで、発火予定から最大 1.2s 遅れていた（実測 +146〜+1184ms）。
//   v2 は「発火タイマー」をメインスレッドが単独で持ち、検知・温めは別スレッドに分離。
//   検知や温めがどれだけ詰まっても、時刻優先なら fire_at ちょうどに撃つ。
//     - 検知スレッド: 解禁を見つけたら発火を前倒しする合図を出すだけ
//     - 温め: パケットごとに mutex。温めが発火に間に合わない台だけが遅れる（全体は待たない）
//       さらに温めのタイムアウトを「発火時刻-300ms」までに自動短縮
//     - 定期再温め: 発火の warm_quiet_ms(既定6000ms)前からは行わない
//   exe 版にあった機能も実装:
//     fire_time_priority / preferred_times / packet.pref_time
//   枠の選び方:
//     1発目  : pref_time(台個別) > preferred_times > 全枠ラウンドロビン（残り人数は見ない）
//     リトライ: 検知で生の枠データ(残り人数)が取れている時だけ、残り人数の多い枠から優先。
//              取れていない時(事前取得の残数しか無い時)は残り人数を使わず、
//              pref_time 台は希望枠に近い枠、それ以外は未試行の枠からランダム。
//     ※ 13時直後の SLOT_CLOSED はトークンが古い可能性があるため、枠を除外しない。
//   1台撃ち終わるごとに stderr へ "@@RES {tag,won,http_status,attempts,latency_ms,fired_at_unix,
//   slot_index,error,body}" を即出力（Python 側が本隊の結果をその場でログに出し、
//   落選した台を解禁+window秒の間すぐ撃ち直すため）。
//
// 入力(stdin, JSON 1メッセージ):
// {
//   "detect": { "url","cookie","headers",{},"impersonate","proxy",
//               "interval_ms":80, "timeout_ms":4000, "max_wait_ms":120000,
//               "slots_key":"slots", "remaining_field":"remaining", "open_min":1,
//               "index_field":"slotIndex", "date_field":"slotDate", "match_date":"YYYY-MM-DD" },
//   "slots":[ {"date","index","time","remaining"} ... ],      // 事前取得枠
//   "fire_attempts":1, "fire_at_unix":T, "fire_time_priority":true,
//   "preferred_times":["13:30",...],
//   "fire_on_timeout":false, "spin_lead_ms":2, "warm_interval_ms":20000, "warm_quiet_ms":6000,
//   "packets":[ { "tag","url","cookie","headers",{},"impersonate","proxy",
//                 "warmup_url","body_template","token","tokens":[...],"pref_time",
//                 "timeout_ms":15000,"connect_timeout_ms":8000 }, ... ]
// }
//   body_template 内の __SLOTDATE__ / __SLOTINDEX__ / __TOKEN__ を発火時に置換する。
//
// 出力(stdout, JSON): { "fired_at_unix":計画発火時刻, "planned_fire_unix":..., "detected", "fired",
//                       "fired_by", "detected_at_unix", "detect_reads", "warm_late",
//                       "open_slots", "count", "results":[ {tag,ok,http_status,latency_ms,attempts,
//                       tokens_n,won,fired_at_unix,slot_index,warm_ms,warm_ok,...} ] }
#include "curl_min.h"
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include <map>
#include <set>
#include <chrono>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <atomic>
#include <sstream>
#include <iostream>
#include <algorithm>
#include <random>
#include <memory>
#include "json.hpp"

#ifdef _WIN32
  #define WIN32_LEAN_AND_MEAN
  #include <windows.h>
#else
  #include <dlfcn.h>
#endif

using namespace std::chrono;
using mj::JValue;
static double now_unix() { return duration<double>(system_clock::now().time_since_epoch()).count(); }
static double mono_ms()  { return duration<double, std::milli>(steady_clock::now().time_since_epoch()).count(); }

// ── libcurl 動的ロード（fire_core と同じ方式）───────────────────────────────
struct CurlLib {
    mj_global_init_t global_init=nullptr; mj_global_cleanup_t global_cleanup=nullptr;
    mj_easy_init_t easy_init=nullptr; mj_easy_setopt_t easy_setopt=nullptr;
    mj_easy_perform_t easy_perform=nullptr; mj_easy_getinfo_t easy_getinfo=nullptr;
    mj_easy_cleanup_t easy_cleanup=nullptr; mj_easy_strerror_t easy_strerror=nullptr;
    mj_slist_append_t slist_append=nullptr; mj_slist_free_all_t slist_free_all=nullptr;
    mj_easy_impersonate_t easy_impersonate=nullptr; bool ok=false; std::string libname;
};
static CurlLib C;
#ifdef _WIN32
static void* lsym(HMODULE m,const char* n){return (void*)GetProcAddress(m,n);}
#else
static void* lsym(void* m,const char* n){return dlsym(m,n);}
#endif
static bool load_curl(CurlLib& c){
#ifdef _WIN32
    const char* cands[]={"libcurl.dll","libcurl-impersonate-chrome.dll","libcurl-impersonate.dll","curl.dll",nullptr};
    HMODULE m=nullptr; for(int i=0;cands[i];i++){m=LoadLibraryA(cands[i]); if(m){c.libname=cands[i];break;}} if(!m)return false;
#else
    const char* cands[]={"libcurl.so","libcurl.so.4","libcurl-impersonate-chrome.so","libcurl-impersonate.so",nullptr};
    void* m=nullptr; for(int i=0;cands[i];i++){m=dlopen(cands[i],RTLD_NOW|RTLD_GLOBAL); if(m){c.libname=cands[i];break;}}
    if(!m)m=dlopen(nullptr,RTLD_NOW); if(!m)return false;
#endif
    c.global_init=(mj_global_init_t)lsym(m,"curl_global_init"); c.global_cleanup=(mj_global_cleanup_t)lsym(m,"curl_global_cleanup");
    c.easy_init=(mj_easy_init_t)lsym(m,"curl_easy_init"); c.easy_setopt=(mj_easy_setopt_t)lsym(m,"curl_easy_setopt");
    c.easy_perform=(mj_easy_perform_t)lsym(m,"curl_easy_perform"); c.easy_getinfo=(mj_easy_getinfo_t)lsym(m,"curl_easy_getinfo");
    c.easy_cleanup=(mj_easy_cleanup_t)lsym(m,"curl_easy_cleanup"); c.easy_strerror=(mj_easy_strerror_t)lsym(m,"curl_easy_strerror");
    c.slist_append=(mj_slist_append_t)lsym(m,"curl_slist_append"); c.slist_free_all=(mj_slist_free_all_t)lsym(m,"curl_slist_free_all");
    c.easy_impersonate=(mj_easy_impersonate_t)lsym(m,"curl_easy_impersonate");
    c.ok=c.global_init&&c.easy_init&&c.easy_setopt&&c.easy_perform&&c.easy_getinfo&&c.easy_cleanup&&c.slist_append&&c.slist_free_all;
    return c.ok;
}
static size_t wcb(char* p,size_t s,size_t n,void* u){((std::string*)u)->append(p,s*n);return s*n;}

static curl_slist* mk_headers(const std::map<std::string,std::string>& H, const std::string& cookie, bool post){
    curl_slist* hl=nullptr; bool ct=false,ck=false;
    for(auto&kv:H){std::string lo;for(char ch:kv.first)lo+=(char)tolower(ch);
        if(lo=="content-type")ct=true; if(lo=="cookie")ck=true;
        hl=C.slist_append(hl,(kv.first+": "+kv.second).c_str());}
    if(!ct&&post) hl=C.slist_append(hl,"Content-Type: application/json");
    if(!ck&&!cookie.empty()) hl=C.slist_append(hl,("Cookie: "+cookie).c_str());
    return hl;
}

struct Packet {
    std::string tag,url,cookie,impersonate,proxy,warmup_url,body_template,token,pref_time;
    std::vector<std::string> tokens;   // 使い捨てトークン対策: 試行ごとに別トークンを使う
    std::map<std::string,std::string> headers;
    int timeout_ms=15000, connect_timeout_ms=8000;
    // runtime
    CURL* h=nullptr; curl_slist* hl=nullptr; std::string resp;
    std::unique_ptr<std::mutex> mu{new std::mutex()};   // ハンドル占有（温め/発火の排他）
    double warm_ms=0; bool warm_ok=false;
    // result
    long http_status=0; double latency_ms=0, fired_at=0; std::string error; bool ok=false, impersonated=false;
    int attempts=0; bool won=false; std::string slot_index;
};

struct Detect {
    std::string url,cookie,impersonate,proxy,slots_key="slots",remaining_field="remaining",
                index_field="slotIndex",date_field="slotDate";
    std::string match_date;
    std::map<std::string,std::string> headers;
    int interval_ms=80, timeout_ms=4000, max_wait_ms=120000, open_min=1;
    // [2] 検知URL並列化: (url, proxy) を複数持てる。最速の1本が解禁を見つけたら発火。
    //   入力 detect.urls=[ "http..." | {"url":..,"proxy":..} ] があればそれを使う。
    //   無ければ単一 detect.url（従来互換）。proxy省略時は detect.proxy を継承。
    std::vector<std::pair<std::string,std::string>> targets;   // (url, proxy)
};

static std::string replace_all(std::string s,const std::string& a,const std::string& b){
    size_t p=0; while((p=s.find(a,p))!=std::string::npos){s.replace(p,a.size(),b);p+=b.size();} return s;
}

static std::string b64_decode(const std::string& in){
    static const std::string tbl=
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out; int val=0,bits=-8;
    for(unsigned char c: in){
        if(c=='=') break;
        size_t idx=tbl.find((char)c);
        if(idx==std::string::npos) continue;
        val=(val<<6)+(int)idx; bits+=6;
        if(bits>=0){ out.push_back(char((val>>bits)&0xFF)); bits-=8; }
    }
    return out;
}
static bool body_has_date(const std::string& body,const std::string& date){
    if(date.empty()) return false;
    if(body.find(date)!=std::string::npos) return true;
    std::string dec=b64_decode(body);
    return !dec.empty() && dec.find(date)!=std::string::npos;
}
static bool body_is_won(const std::string& body){
    return body.find("\"result\":\"won\"")!=std::string::npos
        || body.find("\"result\": \"won\"")!=std::string::npos;
}

struct OpenSlot { std::string date; std::string index; std::string time; long remaining=0; };
static std::vector<OpenSlot> parse_open(const std::string& body, const Detect& d){
    std::vector<OpenSlot> out;
    try{
        mj::JParser pr(body); JValue j=pr.parse();
        const JValue* arr=nullptr;
        if(j.type==JValue::Arr) arr=&j;
        else if(auto s=j.get(d.slots_key); s&&s->type==JValue::Arr) arr=s;
        if(!arr) return out;
        for(auto& it: arr->a){
            if(it.type!=JValue::Obj) continue;
            double rem=0; bool isOpen=true,isFull=false,isPast=false;
            if(auto v=it.get(d.remaining_field)) rem=v->num();
            if(auto v=it.get("isOpen")) isOpen=v->boolean(true);
            if(auto v=it.get("isFull")) isFull=v->boolean(false);
            if(auto v=it.get("isPast")) isPast=v->boolean(false);
            if(isOpen && !isFull && !isPast && rem>=d.open_min){
                OpenSlot os; os.remaining=(long)rem;
                if(auto v=it.get(d.date_field)) os.date=v->str();
                if(auto v=it.get(d.index_field)){ if(v->type==JValue::Num){ long iv=(long)v->num(); os.index=std::to_string(iv);} else os.index=v->str(); }
                if(auto v=it.get("startTime")) os.time=v->str();
                out.push_back(os);
            }
        }
    }catch(...){}
    return out;
}

static void setup_handle(Packet& p){
    p.h=C.easy_init();
    p.hl=mk_headers(p.headers,p.cookie,true);
    p.impersonated=(C.easy_impersonate&&!p.impersonate.empty());
    if(C.easy_impersonate&&!p.impersonate.empty()) C.easy_impersonate(p.h,p.impersonate.c_str(),1);
    C.easy_setopt(p.h,MJ_CURLOPT_HTTPHEADER,p.hl);
    if(!p.proxy.empty()) C.easy_setopt(p.h,MJ_CURLOPT_PROXY,p.proxy.c_str());
    C.easy_setopt(p.h,MJ_CURLOPT_CONNECTTIMEOUT_MS,(long)p.connect_timeout_ms);
    C.easy_setopt(p.h,MJ_CURLOPT_TIMEOUT_MS,(long)p.timeout_ms);
    C.easy_setopt(p.h,MJ_CURLOPT_WRITEFUNCTION,(mj_write_cb)wcb);
    C.easy_setopt(p.h,MJ_CURLOPT_WRITEDATA,(void*)&p.resp);
    C.easy_setopt(p.h,MJ_CURLOPT_ACCEPT_ENCODING,"");
    C.easy_setopt(p.h,MJ_CURLOPT_TCP_NODELAY,1L);
    C.easy_setopt(p.h,MJ_CURLOPT_NOSIGNAL,1L);
}

// 接続を温める（同一ハンドル → keep-alive 保持）。呼び出し側で p.mu を保持していること。
//   limit_ms: このGETに使ってよい最大時間（発火時刻に食い込まないよう呼び出し側で算出）。
static void warm_handle_locked(Packet& p, long limit_ms){
    std::string wurl = !p.warmup_url.empty() ? p.warmup_url : p.url;
    std::string wr;
    long to = std::max(200L, std::min((long)std::min(p.timeout_ms,8000), limit_ms));
    double a=mono_ms();
    C.easy_setopt(p.h,MJ_CURLOPT_URL,wurl.c_str());
    C.easy_setopt(p.h,MJ_CURLOPT_HTTPGET,1L);
    C.easy_setopt(p.h,MJ_CURLOPT_WRITEDATA,(void*)&wr);
    C.easy_setopt(p.h,MJ_CURLOPT_CONNECTTIMEOUT_MS,std::min((long)p.connect_timeout_ms,to));
    C.easy_setopt(p.h,MJ_CURLOPT_TIMEOUT_MS,to);
    CURLcode rc=C.easy_perform(p.h);
    C.easy_setopt(p.h,MJ_CURLOPT_WRITEDATA,(void*)&p.resp);
    C.easy_setopt(p.h,MJ_CURLOPT_CONNECTTIMEOUT_MS,(long)p.connect_timeout_ms);
    C.easy_setopt(p.h,MJ_CURLOPT_TIMEOUT_MS,(long)p.timeout_ms);
    p.warm_ms=mono_ms()-a; p.warm_ok=(rc==MJ_CURLE_OK);
}

static void parse_headers(const JValue& v, std::map<std::string,std::string>& H){
    if(v.type==JValue::Obj) for(auto& kv:v.o) H[kv.first]=kv.second.str();
}

static bool starts_with(const std::string& s, const std::string& pre){
    return !pre.empty() && s.size()>=pre.size() && s.compare(0,pre.size(),pre)==0;
}

int main(){
    std::ostringstream ss; ss<<std::cin.rdbuf(); std::string raw=ss.str();
    JValue j;
    try{ mj::JParser pr(raw); j=pr.parse(); }
    catch(const std::exception& e){ std::cout<<"{\"ok\":false,\"error\":\"parse: "<<e.what()<<"\"}"<<std::endl; return 2; }
    if(!load_curl(C)){ std::cout<<"{\"ok\":false,\"error\":\"libcurl load失敗\"}"<<std::endl; return 3; }
    C.global_init(MJ_CURL_GLOBAL_ALL);

    double spin_lead=2.0, warm_interval=20000.0, warm_quiet=6000.0;
    if(auto v=j.get("spin_lead_ms")) spin_lead=v->num(2.0);
    if(auto v=j.get("warm_interval_ms")) warm_interval=v->num(20000.0);
    if(auto v=j.get("warm_quiet_ms")) warm_quiet=v->num(6000.0);

    Detect D;
    if(auto dv=j.get("detect")){
        if(auto v=dv->get("url")) D.url=v->str();
        if(auto v=dv->get("cookie")) D.cookie=v->str();
        if(auto v=dv->get("impersonate")) D.impersonate=v->str();
        if(auto v=dv->get("proxy")) D.proxy=v->str();
        if(auto v=dv->get("headers")) parse_headers(*v,D.headers);
        if(auto v=dv->get("interval_ms")) D.interval_ms=v->inum(80);
        if(auto v=dv->get("timeout_ms")) D.timeout_ms=v->inum(4000);
        if(auto v=dv->get("max_wait_ms")) D.max_wait_ms=v->inum(120000);
        if(auto v=dv->get("open_min")) D.open_min=v->inum(1);
        if(auto v=dv->get("slots_key")) D.slots_key=v->str(D.slots_key);
        if(auto v=dv->get("remaining_field")) D.remaining_field=v->str(D.remaining_field);
        if(auto v=dv->get("index_field")) D.index_field=v->str(D.index_field);
        if(auto v=dv->get("date_field")) D.date_field=v->str(D.date_field);
        if(auto v=dv->get("match_date")) D.match_date=v->str();
        // [2] 検知URL並列化: detect.urls があれば複数ターゲットを登録。
        //   要素は文字列("http...") か {"url":..,"proxy":..}。proxy省略時は D.proxy を継承。
        if(auto uv=dv->get("urls")){
            if(uv->type==mj::JValue::Arr){
                for(const auto& el : uv->a){
                    std::string u, px=D.proxy;
                    if(el.type==mj::JValue::Str){ u=el.str(); }
                    else if(el.type==mj::JValue::Obj){
                        if(auto t=el.get("url")) u=t->str();
                        if(auto t=el.get("proxy")) px=t->str();
                    }
                    if(!u.empty()) D.targets.emplace_back(u, px);
                }
            }
        }
    }
    // detect.urls が無ければ単一 detect.url を唯一のターゲットに（従来互換）。
    if(D.targets.empty() && !D.url.empty()) D.targets.emplace_back(D.url, D.proxy);

    int fire_attempts=1;
    if(auto v=j.get("fire_attempts")) fire_attempts=v->inum(1);
    if(fire_attempts<1) fire_attempts=1;

    double fire_at = 0;
    if(auto v=j.get("fire_at_unix")) fire_at=v->num(0);
    // 時刻優先: fire_at ちょうどに撃つ。検知は「それより早く解禁を見つけた時の前倒し」にだけ使う。
    bool time_priority=false;
    if(auto v=j.get("fire_time_priority")) time_priority=v->boolean(false);
    bool fire_on_timeout=true;
    if(auto v=j.get("fire_on_timeout")) fire_on_timeout=v->boolean(true);

    std::vector<std::string> preferred_times;
    if(auto pv=j.get("preferred_times"); pv&&pv->type==JValue::Arr)
        for(auto& t: pv->a){ std::string s=t.str(); if(!s.empty()) preferred_times.push_back(s); }
    // retry_remaining_min は廃止（事前取得の残数は解禁前の値のため使わない）。入力にあっても無視。

    std::vector<Packet> pk;
    if(auto pv=j.get("packets"); pv&&pv->type==JValue::Arr){
        for(auto& it: pv->a){
            Packet p;
            if(auto v=it.get("tag")) p.tag=v->str();
            if(auto v=it.get("url")) p.url=v->str();
            if(auto v=it.get("cookie")) p.cookie=v->str();
            if(auto v=it.get("impersonate")) p.impersonate=v->str();
            if(auto v=it.get("proxy")) p.proxy=v->str();
            if(auto v=it.get("warmup_url")) p.warmup_url=v->str();
            if(auto v=it.get("body_template")) p.body_template=v->str();
            if(auto v=it.get("token")) p.token=v->str();
            if(auto v=it.get("pref_time")) p.pref_time=v->str();
            if(auto tv=it.get("tokens"); tv&&tv->type==JValue::Arr){
                for(auto& te: tv->a){ std::string s=te.str(); if(!s.empty()) p.tokens.push_back(s); }
            }
            if(p.tokens.empty() && !p.token.empty()) p.tokens.push_back(p.token);
            if(auto v=it.get("timeout_ms")) p.timeout_ms=v->inum(15000);
            if(auto v=it.get("connect_timeout_ms")) p.connect_timeout_ms=v->inum(8000);
            if(auto v=it.get("headers")) parse_headers(*v,p.headers);
            pk.push_back(std::move(p));
        }
    }
    if(pk.empty()){ std::cout<<"{\"ok\":false,\"error\":\"no packets\"}"<<std::endl; return 2; }

    // 事前取得枠（top-level "slots"）。検知が slots 方式で枠を返した場合はそちらで上書き。
    std::vector<OpenSlot> open_slots;
    if(auto sv=j.get("slots"); sv&&sv->type==JValue::Arr){
        for(auto& it: sv->a){ if(it.type!=JValue::Obj) continue; OpenSlot os;
            if(auto v=it.get("date")) os.date=v->str();
            if(auto v=it.get("index")){ if(v->type==JValue::Num) os.index=std::to_string((long)v->num()); else os.index=v->str(); }
            if(auto v=it.get("time")) os.time=v->str();
            if(auto v=it.get("remaining")) os.remaining=(long)v->num(0);
            if(!os.date.empty()) open_slots.push_back(os);
        }
    }

    const double start_mono=mono_ms();
    // 「ここまでに決着しなければ打ち切り」の壁時計。fire_at はこの範囲に入っている前提。
    const double deadline_unix = now_unix() + D.max_wait_ms/1000.0;

    // 温めに使ってよい残り時間(ms)。発火時刻の300ms手前まで。fire_at 無しなら上限8000。
    auto warm_budget_ms=[&]()->long{
        if(fire_at<=0) return 8000;
        return (long)((fire_at - now_unix())*1000.0 - 300.0);
    };

    // ── 全接続を温める（並行・待たない）──────────────────────────────────────
    //   各スレッドがパケットの mutex を持って温める。発火スレッドは同じ mutex を取るので、
    //   温めが終わっていない台だけがその分遅れ、他の台・発火タイマーは一切待たない。
    std::vector<std::thread> warm_threads;
    for(auto& p: pk){
        setup_handle(p);
        Packet* pp=&p;
        warm_threads.emplace_back([&,pp]{
            std::lock_guard<std::mutex> lk(*pp->mu);
            long b=warm_budget_ms();
            if(b<200) return;            // もう発火直前 → 温めずに冷えたまま撃つ（遅延回避を優先）
            warm_handle_locked(*pp,b);
        });
    }

    std::atomic<bool> detected{false}, stop_bg{false}, live_slots{false};
    std::atomic<long> succ_reads{0};
    std::atomic<double> detect_unix{0.0};
    std::mutex slots_mu;               // 検知スレッドが open_slots を書き換える場合の保護
    std::mutex cv_mu; std::condition_variable cv;

    // ── 接続維持スレッド（定期再温め）──────────────────────────────────────────
    //   発火の warm_quiet_ms 前からは再温めしない（ハンドル占有で発火が遅れるのを防ぐ）。
    std::thread warmer([&]{
        double last=mono_ms();
        while(!detected.load() && !stop_bg.load()){
            std::this_thread::sleep_for(milliseconds(10));
            if(mono_ms()-last < warm_interval) continue;
            for(auto& p: pk){
                if(detected.load()||stop_bg.load()) break;
                if(fire_at>0 && (fire_at-now_unix())*1000.0 < warm_quiet) break;
                std::unique_lock<std::mutex> lk(*p.mu, std::try_to_lock);
                if(!lk.owns_lock()) continue;
                long b=warm_budget_ms(); if(b<200) break;
                warm_handle_locked(p,std::min(b,4000L));
            }
            last=mono_ms();
        }
    });

    // ── 検知スレッド（[2] URL/経路ごとに1本。最初に解禁を見つけた1本が発火を促す）──
    //   GETが詰まっても影響するのはそのスレッドだけ。発火タイマー（メイン）は止まらない。
    //   detected はターゲット間で共有。open_slots/live_slots は slots_mu で保護され先勝ち。
    auto detect_work = [&](std::string durl, std::string dproxy){
        CURL* dh=C.easy_init();
        std::map<std::string,std::string> dH=D.headers; dH["Cache-Control"]="no-cache"; dH["Pragma"]="no-cache";
        curl_slist* dhl=mk_headers(dH,D.cookie,false);
        if(C.easy_impersonate&&!D.impersonate.empty()) C.easy_impersonate(dh,D.impersonate.c_str(),1);
        C.easy_setopt(dh,MJ_CURLOPT_HTTPHEADER,dhl);
        if(!dproxy.empty()) C.easy_setopt(dh,MJ_CURLOPT_PROXY,dproxy.c_str());
        C.easy_setopt(dh,MJ_CURLOPT_CONNECTTIMEOUT_MS,(long)D.timeout_ms);
        C.easy_setopt(dh,MJ_CURLOPT_TIMEOUT_MS,(long)D.timeout_ms);
        C.easy_setopt(dh,MJ_CURLOPT_WRITEFUNCTION,(mj_write_cb)wcb);
        C.easy_setopt(dh,MJ_CURLOPT_ACCEPT_ENCODING,"");
        C.easy_setopt(dh,MJ_CURLOPT_TCP_NODELAY,1L);
        C.easy_setopt(dh,MJ_CURLOPT_NOSIGNAL,1L);
        bool has_q = (durl.find('?')!=std::string::npos);
        long cb=0;
        while(!detected.load() && !stop_bg.load() && now_unix()<deadline_unix){
            std::string u = durl + (has_q?"&_=":"?_=") + std::to_string(++cb) + "t"
                          + std::to_string((long long)mono_ms());
            C.easy_setopt(dh,MJ_CURLOPT_URL,u.c_str());
            std::string db; C.easy_setopt(dh,MJ_CURLOPT_WRITEDATA,(void*)&db);
            CURLcode rc=C.easy_perform(dh);
            if(stop_bg.load()||detected.load()) break;
            if(rc==MJ_CURLE_OK){
                succ_reads++;
                bool hit=false;
                if(!D.match_date.empty()){
                    hit=body_has_date(db,D.match_date);
                } else {
                    auto os=parse_open(db,D);
                    if(!os.empty()){ std::lock_guard<std::mutex> lk(slots_mu); if(!live_slots.load()){ open_slots=os; live_slots.store(true);} hit=true; }
                }
                if(hit){
                    bool was=detected.exchange(true);
                    if(!was) detect_unix.store(now_unix());
                    { std::lock_guard<std::mutex> lk(cv_mu); }
                    cv.notify_all();
                    break;
                }
            }
            for(int k=0;k<D.interval_ms && !stop_bg.load() && !detected.load();k+=5)
                std::this_thread::sleep_for(milliseconds(5));
        }
        if(dhl)C.slist_free_all(dhl); if(dh)C.easy_cleanup(dh);
    };
    std::vector<std::thread> detectors;
    for(const auto& tg : D.targets)
        detectors.emplace_back(detect_work, tg.first, tg.second);

    // ── 発火判定（メインスレッド＝発火タイマー）────────────────────────────────
    //   wake_at: 時刻で撃つ候補時刻。ここに来たら判定し、撃つなら fire_at へ busy-wait。
    const char* fired_by="trigger";
    bool do_fire=false;
    double t_fire=0;
    const double PRE_WAKE=0.030;   // 発火30ms前に起きてスレッド生成・busy-waitへ
    if(D.targets.empty()){
        // 検知ターゲット無し → 即発火（Pythonトリガ運用・B案互換）
        detected.store(true); do_fire=true; fired_by="trigger";
        t_fire = (fire_at>0 ? std::max(fire_at, now_unix()+0.02) : now_unix()+0.02);
    } else {
        while(true){
            double now=now_unix();
            if(detected.load()){
                do_fire=true; fired_by="detect";
                // 時刻優先で検知が fire_at より遅れた場合は即。早ければ前倒し(now+20ms)。
                t_fire = now + 0.02;
                break;
            }
            if(fire_at>0 && now >= fire_at - PRE_WAKE){
                if(time_priority){
                    do_fire=true; fired_by="time"; t_fire=std::max(fire_at, now+0.002); break;
                }
                // 旧仕様: 検知エンドポイントが死んでいる(読み取り0)時だけ盲撃ち
                if(succ_reads.load()==0){
                    do_fire=true; fired_by="time"; t_fire=std::max(fire_at, now+0.002); break;
                }
            }
            if(now >= deadline_unix) break;
            // 次の判定まで眠る。検知があれば cv で即起床。fire_at 前は細かく刻む。
            double until = deadline_unix;
            if(fire_at>0 && now < fire_at - PRE_WAKE) until = std::min(until, fire_at - PRE_WAKE);
            double ms = std::max(0.5, std::min(50.0, (until-now)*1000.0));
            std::unique_lock<std::mutex> lk(cv_mu);
            cv.wait_for(lk, microseconds((long long)(ms*1000.0)), [&]{ return detected.load(); });
        }
        if(!do_fire && fire_on_timeout){ do_fire=true; fired_by="trigger"; t_fire=now_unix()+0.02; }
    }
    stop_bg.store(true);

    if(!do_fire){
        for(auto& t: detectors) if(t.joinable()) t.join();
        if(warmer.joinable()) warmer.join();
        for(auto& t: warm_threads) if(t.joinable()) t.join();
        std::cout<<"{\"detected\":false,\"fired\":false,\"open_slots\":0,\"count\":"<<pk.size()
                 <<",\"detect_reads\":"<<succ_reads.load()<<",\"results\":[]}"<<std::endl;
        for(auto& p: pk){ if(p.hl)C.slist_free_all(p.hl); if(p.h)C.easy_cleanup(p.h); }
        if(C.global_cleanup)C.global_cleanup();
        return 0;
    }

    // ── 枠割当 ──────────────────────────────────────────────────────────────
    std::vector<OpenSlot> slots;
    { std::lock_guard<std::mutex> lk(slots_mu); slots=open_slots; }
    // 残り人数が「検知で取れた生データ」か（事前取得の値は解禁前のものなので使わない）
    const bool live_remaining = live_slots.load();
    // 1発目: pref_time(台個別) > preferred_times(全体) > 全枠ラウンドロビン
    std::vector<size_t> pref_pool;
    for(size_t s=0;s<slots.size();s++)
        for(auto& t: preferred_times) if(starts_with(slots[s].time,t)){ pref_pool.push_back(s); break; }
    std::vector<long> first_slot(pk.size(), -1);
    {
        size_t rr_all=0, rr_pref=0;
        for(size_t i=0;i<pk.size();i++){
            if(slots.empty()) break;
            long pick=-1;
            if(!pk[i].pref_time.empty())
                for(size_t s=0;s<slots.size();s++) if(starts_with(slots[s].time,pk[i].pref_time)){ pick=(long)s; break; }
            if(pick<0 && !pref_pool.empty()) pick=(long)pref_pool[rr_pref++ % pref_pool.size()];
            if(pick<0) pick=(long)(rr_all++ % slots.size());
            first_slot[i]=pick;
        }
    }

    int warm_late=0;
    for(auto& p: pk){ std::unique_lock<std::mutex> lk(*p.mu, std::try_to_lock); if(!lk.owns_lock()) warm_late++; }

    auto busy=[&](double at){ while(true){double r=(at-now_unix())*1000.0; if(r<=spin_lead)break;
        std::this_thread::sleep_for(microseconds((long long)(std::min(r-spin_lead,20.0)*1000)));} while(now_unix()<at){} };

    std::mutex res_mu;
    std::vector<std::thread> ft;
    for(size_t i=0;i<pk.size();i++){
        ft.emplace_back([&,i]{
            Packet& p=pk[i];
            std::mt19937 rng((unsigned)(i*7919u + (unsigned)(mono_ms())));
            busy(t_fire);
            std::lock_guard<std::mutex> lk(*p.mu);    // 温め中の台だけはここで待つ（他台は無関係）
            C.easy_setopt(p.h,MJ_CURLOPT_URL,p.url.c_str());
            C.easy_setopt(p.h,MJ_CURLOPT_HTTPGET,0L);
            C.easy_setopt(p.h,MJ_CURLOPT_POST,1L);
            std::set<size_t> tried;
            long cur = first_slot[i];
            for(int att=0; att<fire_attempts; att++){
                if(att>0 && !slots.empty()){
                    // リトライ枠: 既試行を除外。検知済みなら残り人数の多い枠から、
                    //   未検知なら pref_time 台は希望枠に近い枠、それ以外はランダム。
                    std::vector<size_t> cand;
                    for(size_t s=0;s<slots.size();s++) if(!tried.count(s)) cand.push_back(s);
                    if(cand.empty() || live_remaining){ cand.clear(); for(size_t s=0;s<slots.size();s++) cand.push_back(s); }
                    if(live_remaining){
                        // 検知済み: 残り人数が一番多い枠（同数ならランダム）
                        std::shuffle(cand.begin(),cand.end(),rng);
                        std::stable_sort(cand.begin(),cand.end(),[&](size_t a,size_t b){
                            return slots[a].remaining > slots[b].remaining; });
                        cur=(long)cand[0];
                    } else if(!p.pref_time.empty() && first_slot[i]>=0){
                        long base=first_slot[i];
                        std::stable_sort(cand.begin(),cand.end(),[&](size_t a,size_t b){
                            return std::labs((long)a-base) < std::labs((long)b-base); });
                        cur=(long)cand[0];
                    } else {
                        cur=(long)cand[rng()%cand.size()];
                    }
                }
                std::string body=p.body_template;
                if(cur>=0){
                    const OpenSlot& os=slots[(size_t)cur];
                    body=replace_all(body,"__SLOTDATE__",os.date);
                    body=replace_all(body,"__SLOTINDEX__",os.index);
                    tried.insert((size_t)cur);
                    if(att==0) p.slot_index=os.index;
                }
                std::string tk = p.tokens.empty() ? p.token
                               : p.tokens[ std::min((size_t)att, p.tokens.size()-1) ];
                std::string b2=replace_all(body,"__TOKEN__",tk);
                C.easy_setopt(p.h,MJ_CURLOPT_POSTFIELDS,b2.c_str());
                C.easy_setopt(p.h,MJ_CURLOPT_POSTFIELDSIZE,(long)b2.size());
                if(att==0) p.fired_at=now_unix();
                p.resp.clear();
                double a=mono_ms(); CURLcode rc=C.easy_perform(p.h); p.latency_ms=mono_ms()-a;
                p.attempts=att+1;
                if(rc!=MJ_CURLE_OK){ p.error=C.easy_strerror?C.easy_strerror(rc):"curl error"; p.http_status=0; p.ok=false; }
                else { C.easy_getinfo(p.h,MJ_CURLINFO_RESPONSE_CODE,&p.http_status); p.ok=true; p.error.clear(); }
                p.resp=p.resp.substr(0,8192);
                if(p.ok && body_is_won(p.resp)){ p.won=true; break; }
            }
            // 1台撃ち終わるごとに即通知（Python側の追い撃ちが落選台をすぐ撃ち直せるように）
            //   Python 側はこの行でそのまま本隊の結果をログに出す（全台の完了を待たない）。
            { std::ostringstream ln;
              ln<<"@@RES {\"tag\":"; mj::json_escape(ln,p.tag);
              ln<<",\"won\":"<<(p.won?"true":"false")
                <<",\"http_status\":"<<p.http_status
                <<",\"attempts\":"<<p.attempts
                <<",\"latency_ms\":"<<std::fixed<<p.latency_ms
                <<",\"fired_at_unix\":"<<std::fixed<<p.fired_at
                <<",\"slot_index\":"; mj::json_escape(ln,p.slot_index);
              ln<<",\"error\":"; mj::json_escape(ln,p.error);
              ln<<",\"body\":"; mj::json_escape(ln,p.resp.substr(0,400));
              ln<<"}\n";
              std::string sline=ln.str();
              std::lock_guard<std::mutex> lk3(res_mu);
              std::fwrite(sline.data(),1,sline.size(),stderr);
              std::fflush(stderr); }
        });
    }
    for(auto& t: ft) t.join();

    // 背景スレッドの後始末（検知GETが詰まっていても結果の出力は先に済ませる）
    std::ostringstream out;
    out<<"{\"fired_at_unix\":"<<std::fixed<<t_fire<<",\"planned_fire_unix\":"<<std::fixed<<fire_at
       <<",\"detected\":"<<(detected.load()?"true":"false")
       <<",\"fired\":true,\"fired_by\":\""<<fired_by<<"\""
       <<",\"detected_at_unix\":"<<std::fixed<<detect_unix.load()
       <<",\"detect_reads\":"<<succ_reads.load()
       <<",\"warm_late\":"<<warm_late
       <<",\"live_slots\":"<<(live_slots.load()?"true":"false")
       <<",\"engine_ms\":"<<(mono_ms()-start_mono)
       <<",\"open_slots\":"<<slots.size()<<",\"count\":"<<pk.size()<<",\"results\":[";
    for(size_t i=0;i<pk.size();i++){ auto&p=pk[i]; if(i)out<<",";
        out<<"{\"tag\":"; mj::json_escape(out,p.tag);
        out<<",\"ok\":"<<(p.error.empty()?"true":"false")
           <<",\"http_status\":"<<p.http_status
           <<",\"latency_ms\":"<<p.latency_ms
           <<",\"attempts\":"<<p.attempts
           <<",\"tokens_n\":"<<p.tokens.size()
           <<",\"won\":"<<(p.won?"true":"false")
           <<",\"fired_at_unix\":"<<std::fixed<<p.fired_at
           <<",\"warm_ms\":"<<p.warm_ms
           <<",\"warm_ok\":"<<(p.warm_ok?"true":"false")
           <<",\"impersonated\":"<<(p.impersonated?"true":"false")
           <<",\"slot_index\":"; mj::json_escape(out,p.slot_index);
        out<<",\"error\":"; mj::json_escape(out,p.error);
        out<<",\"body\":"; mj::json_escape(out,p.resp); out<<"}";
    }
    out<<"]}";
    std::cout<<out.str()<<std::endl;
    std::cout.flush();

    for(auto& t: detectors) if(t.joinable()) t.join();
    if(warmer.joinable()) warmer.join();
    for(auto& t: warm_threads) if(t.joinable()) t.join();
    for(auto& p: pk){ if(p.hl)C.slist_free_all(p.hl); if(p.h)C.easy_cleanup(p.h); }
    if(C.global_cleanup)C.global_cleanup();
    return 0;
}
