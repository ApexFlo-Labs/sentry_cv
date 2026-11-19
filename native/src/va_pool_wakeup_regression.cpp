// Diagnostic only, exact verified intel-dlstreamer2026.2.0 amd64 binary.
// Exercise its real LOCAL Acquire/Release methods through their verified ELF
// offsets, using CPU-owned test objects. No VA/model/media calls. A test-only
// condition-wait barrier schedules release between predicate check and wait.
#define _GNU_SOURCE
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <dlfcn.h>
#include <iostream>
#include <link.h>
#include <mutex>
#include <pthread.h>
#include <stdexcept>
#include <thread>
#include <vector>
#include <cerrno>

struct TestImage { unsigned char unused[0x98]{}; bool completed=false; };
struct TestPool { std::vector<TestImage*> images; std::condition_variable ready; std::mutex mutex; };
static_assert(offsetof(TestImage,completed)==0x98);
static_assert(offsetof(TestPool,ready)==0x18);
static_assert(offsetof(TestPool,mutex)==0x48);
using Acquire=TestImage*(*)(TestPool*);
using Release=void(*)(TestPool*,TestImage*);
std::atomic<pthread_cond_t*> target{nullptr};
std::atomic<bool> at_wait{false}, release_attempted{false},release_finished{false};
std::atomic<int> intercepted{0},missed{0};

extern "C" int pthread_cond_wait(pthread_cond_t* cond,pthread_mutex_t* mutex) {
    auto real=reinterpret_cast<int(*)(pthread_cond_t*,pthread_mutex_t*)>(dlsym(RTLD_NEXT,"pthread_cond_wait"));
    if(cond!=target.load())return real(cond,mutex);
    intercepted++;at_wait=true;
    auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(1);
    while(!release_attempted.load() && std::chrono::steady_clock::now()<deadline)std::this_thread::yield();
    // Original Release does not take this held mutex, and finishes here. A
    // corrected Release cannot finish until the condition wait unlocks it.
    std::this_thread::sleep_for(std::chrono::milliseconds(15));
    timespec until{};clock_gettime(CLOCK_REALTIME,&until);until.tv_nsec+=100000000;
    if(until.tv_nsec>=1000000000){until.tv_sec++;until.tv_nsec-=1000000000;}
    int rc=pthread_cond_timedwait(cond,mutex,&until);
    if(rc==ETIMEDOUT){missed++;return 0;} // bounded diagnostic wake; no production workaround
    return rc;
}

void synchronized_release(TestPool* pool,TestImage* image){
    {std::lock_guard<std::mutex> guard(pool->mutex);image->completed=true;}
    pool->ready.notify_one();
}

int exercise(Acquire acquire,Release release){
    TestImage image;TestPool pool;pool.images.push_back(&image);
    at_wait=false;release_attempted=false;release_finished=false;intercepted=0;missed=0;
    target=pool.ready.native_handle();
    std::thread producer([&]{
        auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
        while(!at_wait && std::chrono::steady_clock::now()<deadline)std::this_thread::yield();
        release_attempted=true;release(&pool,&image);release_finished=true;
    });
    TestImage* result=acquire(&pool);producer.join();target=nullptr;
    if(result!=&image || !release_finished || intercepted!=1)throw std::runtime_error("native ABI/wait hook mismatch");
    return missed;
}

int main(int argc,char**argv){
    if(argc!=2)return 2;
    void* library=dlopen(argv[1],RTLD_LAZY|RTLD_LOCAL);
    if(!library){std::cerr<<dlerror()<<"\n";return 2;}
    link_map* info=nullptr;if(dlinfo(library,RTLD_DI_LINKMAP,&info)!=0)return 2;
    // Addresses proven by disassembly of SHA256
    // be76f7c50ade5ad6cf22b485bd1963b79856faf504a501d0d99ceadd312851ac.
    auto acquire=reinterpret_cast<Acquire>(info->l_addr+0x316370);
    auto release=reinterpret_cast<Release>(info->l_addr+0x316480);
    int original=exercise(acquire,release),fixed=exercise(acquire,synchronized_release);
    std::cout<<"{\"actual_sdk_missed_notification\":"<<original
             <<",\"synchronized_release_missed_notification\":"<<fixed
             <<",\"scope\":\"Controlled real SDK methods with test-only pre-wait barrier; no deployment root-cause or capacity claim\"}\n";
    return original==1 && fixed==0?0:3;
}
