# Add project specific ProGuard rules here.
# Keep JNI bridge classes
-keep class com.samurai.shugocore.inference.LlamaCppBridge { *; }
-keep class com.samurai.shugocore.inference.LocalApiServer { *; }
-keep class com.samurai.shugocore.ShugoCoreService { *; }
-keep class com.samurai.shugocore.MainActivity { *; }

# Keep native methods
-keepclasseswithmembernames class * {
    native <methods>;
}

# Keep Chaquopy
-keep class com.chaquo.python.** { *; }
-dontwarn com.chaquo.python.**