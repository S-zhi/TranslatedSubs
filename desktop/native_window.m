#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>

@interface AppDelegate : NSObject <NSApplicationDelegate>
@property(nonatomic, copy) NSString *urlString;
@property(nonatomic, copy) NSString *windowTitle;
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) WKWebView *webView;
- (instancetype)initWithURL:(NSString *)url title:(NSString *)title;
@end

@implementation AppDelegate

- (instancetype)initWithURL:(NSString *)url title:(NSString *)title {
    self = [super init];
    if (self) {
        _urlString = [url copy];
        _windowTitle = [title copy];
    }
    return self;
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    NSURL *url = [NSURL URLWithString:self.urlString];
    if (!url) {
        [NSApp terminate:nil];
        return;
    }

    WKWebViewConfiguration *configuration = [WKWebViewConfiguration new];
    self.webView = [[WKWebView alloc] initWithFrame:NSZeroRect
                                      configuration:configuration];
    self.webView.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;

    self.window = [[NSWindow alloc]
        initWithContentRect:NSMakeRect(0, 0, 1440, 920)
                  styleMask:(NSWindowStyleMaskTitled |
                             NSWindowStyleMaskClosable |
                             NSWindowStyleMaskMiniaturizable |
                             NSWindowStyleMaskResizable)
                    backing:NSBackingStoreBuffered
                      defer:NO];
    self.window.title = self.windowTitle;
    self.window.minSize = NSMakeSize(1024, 700);
    self.window.contentView = self.webView;
    [self.window center];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];

    [self.webView loadRequest:[NSURLRequest requestWithURL:url]];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender {
    return YES;
}

@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSString *urlString = nil;
        NSString *windowTitle = @"TranslatedSubs";
        for (int index = 1; index < argc; index += 1) {
            NSString *argument = [NSString stringWithUTF8String:argv[index]];
            if ([argument isEqualToString:@"--url"] && index + 1 < argc) {
                urlString = [NSString stringWithUTF8String:argv[++index]];
            } else if ([argument isEqualToString:@"--title"] && index + 1 < argc) {
                windowTitle = [NSString stringWithUTF8String:argv[++index]];
            }
        }

        if (!urlString) {
            fprintf(stderr, "Usage: TranslatedSubsWindow --url URL [--title TITLE]\n");
            return 2;
        }

        NSApplication *application = [NSApplication sharedApplication];
        [application setActivationPolicy:NSApplicationActivationPolicyRegular];
        AppDelegate *delegate = [[AppDelegate alloc] initWithURL:urlString
                                                            title:windowTitle];
        application.delegate = delegate;
        [application run];
    }
    return 0;
}
