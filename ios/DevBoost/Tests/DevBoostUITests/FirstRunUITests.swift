import XCTest

@MainActor
final class FirstRunUITests: XCTestCase {
    private func launchFresh() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["--ui-test-reset-state"]
        app.launch()
        return app
    }

    func testFirstRunShowsAllMainDestinations() {
        let app = launchFresh()
        XCTAssertTrue(app.tabBars.buttons["Home"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.tabBars.buttons["Hosts"].exists)
        XCTAssertTrue(app.tabBars.buttons["Docker"].exists)
        XCTAssertTrue(app.tabBars.buttons["Transfer"].exists)
        XCTAssertTrue(app.tabBars.buttons["Usage"].exists)
    }

    func testFirstRunEmptyStatesExplainWhatToDoNext() {
        let app = launchFresh()
        app.tabBars.buttons["Hosts"].tap()
        XCTAssertTrue(app.staticTexts["No SSH hosts"].waitForExistence(timeout: 5))

        app.tabBars.buttons["Docker"].tap()
        XCTAssertTrue(app.staticTexts["No SSH hosts"].waitForExistence(timeout: 5))

        app.tabBars.buttons["Transfer"].tap()
        XCTAssertTrue(app.staticTexts["No SSH hosts"].waitForExistence(timeout: 5))

        app.tabBars.buttons["Usage"].tap()
        XCTAssertTrue(app.staticTexts["No SSH hosts"].waitForExistence(timeout: 5))
    }

    func testFirstTimeHostSetupValidatesConnectionFields() {
        let app = launchFresh()
        app.tabBars.buttons["Hosts"].tap()
        app.buttons["Add SSH host"].tap()

        let save = app.buttons["Save"]
        XCTAssertTrue(save.waitForExistence(timeout: 5))
        XCTAssertFalse(save.isEnabled)
        app.textFields["Hostname or IP"].tap()
        app.textFields["Hostname or IP"].typeText("server.example")
        app.textFields["Username"].tap()
        app.textFields["Username"].typeText("ubuntu")
        XCTAssertTrue(save.isEnabled)
    }
}
